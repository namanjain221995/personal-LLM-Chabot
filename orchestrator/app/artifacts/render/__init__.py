"""The renderers: an ArtifactSpec in, real files out, every one reopened.

    render_version(spec, formats, out_dir, title_slug=..., version=..., effort=...)
        → RenderReport(files, preview_pdf, preview_kind, preview_pages, warnings, validation, chart_files, timings)

ORDER OF WORK. Charts first (PNG files into `out_dir`, referenced by bare
filename from the HTML and embedded by the DOCX); then every requested
format; then `preview.pdf` — for a document the PDF itself when one was
requested, else a PDF from the same spec; for a presentation a PDF whose
pages ARE the 16:9 slides drawn from the same plan as the .pptx (never a
screenshot of the .pptx); for a workbook a short summary PDF so the card
has a thumbnail, with `preview_kind='grid'` because the real preview is the
sheet grid. Then validate.py reopens everything it wrote.

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
class RenderReport:
    files: List[T.FileRef] = field(default_factory=list)
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

    def to_json(self) -> dict:
        return {
            "files": [f.to_json() for f in self.files],
            "preview_pdf": self.preview_pdf,
            "preview_kind": self.preview_kind,
            "preview_pages": self.preview_pages,
            "warnings": list(self.warnings),
            "validation": self.validation,
            "chart_files": list(self.chart_files),
            "timings": {k: round(float(v), 4) for k, v in self.timings.items()},
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
    return out


def _import_failure(fmt: str) -> RenderError:
    return RenderError("dependency_unavailable", f"The {fmt.upper()} writer is not installed on this server.")


def _safe(fmt: str, what: str) -> RenderError:
    return RenderError("renderer_failure", f"The {fmt.upper()} could not be built: {what}.")


#: What types.slug_for() produces; anything else in a filename stem is a
#: caller's bug, and a "/" or ".." would write outside out_dir.
_SLUG_RE = re.compile(r"[a-z0-9-]{1,60}")


@contextlib.contextmanager
def _timed(report: RenderReport, key: str):
    """`with _timed(report, "pdf"):` adds the block's seconds to report.timings."""
    started = time.perf_counter()
    try:
        yield
    finally:
        report.timings[key] = report.timings.get(key, 0.0) + (time.perf_counter() - started)


def render_version(spec: S.ArtifactSpec, formats: Sequence[str], out_dir: str, *, title_slug: str, version: int,
                   effort: str = "fast") -> RenderReport:
    """Render `spec` to every format in `formats` under `out_dir`, plus
    preview.pdf, validate them all, and describe the result."""
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

    # 2. Formats, then 3. preview.pdf.
    paths: Dict[str, Path] = {}
    body = spec.body
    if isinstance(body, S.DocumentSpec):
        plan = H.plan_document(body)
        report.warnings.extend(plan.warnings)
        html = H.document_html(body, plan)
        if "pdf" in wanted:
            with timed("pdf"):
                paths["pdf"] = _render_pdf(html, out, f"{title_slug}-v{version}.pdf", "pdf")
                _warn_pages(plan, paths["pdf"], report)
        if "docx" in wanted:
            with timed("docx"):
                paths["docx"] = _render_docx(body, plan, out, f"{title_slug}-v{version}.docx", report)
        with timed("preview"):
            preview = _preview_from(paths, html, out)
        report.preview_kind = "pages"
    elif isinstance(body, S.PresentationSpec):
        plan = H.plan_deck(body)
        report.warnings.extend(plan.warnings)
        html = H.deck_html(body, plan)
        if "pptx" in wanted:
            with timed("pptx"):
                paths["pptx"] = _render_pptx(body, plan, out, f"{title_slug}-v{version}.pptx")
        if "pdf" in wanted:
            with timed("pdf"):
                paths["pdf"] = _render_pdf(html, out, f"{title_slug}-v{version}.pdf", "pdf")
        with timed("preview"):
            preview = _preview_from(paths, html, out)
        report.preview_kind = "pages"
    elif isinstance(body, S.WorkbookSpec):
        with timed("xlsx"):
            paths["xlsx"] = _render_xlsx(body, out, f"{title_slug}-v{version}.xlsx", report)
        with timed("preview"):
            preview = _render_pdf(H.workbook_summary_html(body), out, T.PREVIEW_PDF_NAME, "preview")
        report.preview_kind = "grid"
    else:  # pragma: no cover - the spec envelope admits only the three kinds
        raise RenderError("invalid_request", f"Unknown artifact kind {spec.kind!r}.")

    # 4. Validate by reopening.
    try:
        validation = V.validate_all({fmt: str(p) for fmt, p in paths.items()}, str(preview))
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

    for fmt in wanted:
        p = paths[fmt]
        facts = validation["files"][fmt]
        report.files.append(T.FileRef(
            format=fmt, filename=p.name, mime_type=T.MIME_TYPES[fmt], size=int(facts["size"]),
            sha256=str(facts["sha256"]), pages=facts.get("pages"), slides=facts.get("slides"), sheets=facts.get("sheets"),
        ))
    report.preview_pdf = str(preview)
    report.preview_pages = int(validation["preview"]["pages"])
    if isinstance(body, S.PresentationSpec) and report.preview_pages != len(plan.slides):
        raise RenderError("validation_failure", "The slide preview does not have one page per slide.")
    return report


def _preview_from(paths: Dict[str, Path], html: str, out: Path) -> Path:
    """preview.pdf: a copy of the requested PDF, else a render of the same
    HTML — one spec, one plan, one look."""
    preview = out / T.PREVIEW_PDF_NAME
    if "pdf" in paths:
        preview.write_bytes(paths["pdf"].read_bytes())
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


def _render_pdf(html: str, out: Path, name: str, label: str) -> Path:
    from .pdf import render_html_pdf

    target = out / name
    try:
        render_html_pdf(html, target, out)
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


__all__ = ["RenderError", "RenderReport", "render_version", "capabilities"]
