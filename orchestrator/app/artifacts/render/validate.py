"""Reopen every rendered file and say what it is — the `validate` stage.

WHY. The legacy report path advertised a file on `is_file()` alone
(CURRENT_STATE.md: "no %PDF magic, page count, zip integrity, DOCX/XLSX
reopen"). A truncated PDF or a zip that lost its central directory reaches
the person as a card whose download opens nothing. Here each output is
reopened with the library that would read it — pypdfium2, python-docx,
python-pptx, openpyxl — and its structural facts (pages, slides, sheets,
size, sha256) are recorded in validation.json.

WHAT IS REFUSED. An Office file with a `vbaProject.bin` part (a macro) or a
relationship whose TargetMode is External (a link that resolves outside the
package — the classic remote-template phone-home) fails validation; neither
can come from python-docx/pptx/openpyxl writing our spec, so their presence
means the file is not the one we wrote. A PDF without the `%PDF` header,
with zero pages, over types.MAX_PAGES, or with a page whose media box is
empty fails too. A validation failure is a RenderError('validation_failure')
upstream, never a card.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path
from typing import Dict, Optional

from .. import types as T

_EXTERNAL_TARGET_RE = re.compile(rb'TargetMode\s*=\s*"External"', re.IGNORECASE)


class ValidationFailed(ValueError):
    """The file is not one we can release. The message is safe to show."""


def sha256_of(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_zip(path: Path) -> None:
    """zipfile integrity, no macro part, no external relationship target."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValidationFailed("the file is not a valid Office package") from exc
    with zf:
        bad = zf.testzip()
        if bad is not None:
            raise ValidationFailed("the Office package has a corrupt part")
        names = zf.namelist()
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise ValidationFailed("the file carries a macro project, which is never produced here")
        for name in names:
            if name.endswith(".rels"):
                if _EXTERNAL_TARGET_RE.search(zf.read(name)):
                    raise ValidationFailed("the file links to an external target, which is never produced here")


def validate_pdf(path: str | Path, *, max_pages: int = T.MAX_PAGES) -> Dict[str, object]:
    import pypdfium2 as pdfium  # lazy

    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(5)
    if head != b"%PDF-":
        raise ValidationFailed("the PDF does not start with a PDF header")
    try:
        pdf = pdfium.PdfDocument(str(p))
    except Exception as exc:  # pypdfium2 raises its own error type on a broken file
        raise ValidationFailed("the PDF could not be opened") from exc
    try:
        pages = len(pdf)
        if pages <= 0:
            raise ValidationFailed("the PDF has no pages")
        if pages > max_pages:
            raise ValidationFailed(f"the PDF has {pages} pages; the ceiling is {max_pages}")
        for i in range(pages):
            page = pdf[i]
            try:
                w, h = page.get_size()
            finally:
                page.close()
            if w <= 0 or h <= 0:
                raise ValidationFailed(f"page {i + 1} of the PDF has an empty media box")
    finally:
        pdf.close()
    return {"ok": True, "pages": pages, "size": p.stat().st_size}


def validate_docx(path: str | Path) -> Dict[str, object]:
    from docx import Document  # lazy

    p = Path(path)
    _check_zip(p)
    try:
        document = Document(str(p))
    except Exception as exc:
        raise ValidationFailed("the DOCX could not be opened by Word's library") from exc
    paragraphs = len(document.paragraphs)
    if paragraphs == 0 and not document.tables:
        raise ValidationFailed("the DOCX is empty")
    return {"ok": True, "paragraphs": paragraphs, "tables": len(document.tables), "size": p.stat().st_size}


def validate_pptx(path: str | Path) -> Dict[str, object]:
    from pptx import Presentation  # lazy

    p = Path(path)
    _check_zip(p)
    try:
        prs = Presentation(str(p))
    except Exception as exc:
        raise ValidationFailed("the PPTX could not be opened by PowerPoint's library") from exc
    slides = len(prs.slides)
    if slides <= 0:
        raise ValidationFailed("the PPTX has no slides")
    if slides > T.MAX_SLIDES + 1:  # + the sources slide
        raise ValidationFailed(f"the PPTX has {slides} slides; the ceiling is {T.MAX_SLIDES}")
    return {"ok": True, "slides": slides, "size": p.stat().st_size}


def validate_xlsx(path: str | Path) -> Dict[str, object]:
    from openpyxl import load_workbook  # lazy

    p = Path(path)
    _check_zip(p)
    try:
        wb = load_workbook(str(p), read_only=True)
    except Exception as exc:
        raise ValidationFailed("the XLSX could not be opened by Excel's library") from exc
    try:
        names = list(wb.sheetnames)
    finally:
        wb.close()
    if not names:
        raise ValidationFailed("the XLSX has no sheets")
    return {"ok": True, "sheets": len(names), "sheet_names": names, "size": p.stat().st_size}


_VALIDATORS = {"pdf": validate_pdf, "docx": validate_docx, "pptx": validate_pptx, "xlsx": validate_xlsx}


def validate_file(path: str | Path, fmt: str) -> Dict[str, object]:
    """Reopen `path` as `fmt`; return the facts, or raise ValidationFailed."""
    if fmt not in _VALIDATORS:
        raise ValidationFailed(f"no validator for {fmt}")
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        raise ValidationFailed(f"the {fmt} file was not written")
    if p.stat().st_size > T.MAX_FILE_BYTES:
        raise ValidationFailed(f"the {fmt} file is larger than {T.MAX_FILE_BYTES // (1024 * 1024)} MB")
    facts = _VALIDATORS[fmt](p)
    facts["sha256"] = sha256_of(p)
    facts["format"] = fmt
    facts["filename"] = p.name
    return facts


def validate_all(files: Dict[str, str], preview_pdf: Optional[str] = None) -> Dict[str, object]:
    """{fmt: path} (+ the preview) → the validation.json dict. Raises on
    the first failure; the caller turns it into a RenderError."""
    out: Dict[str, object] = {"files": {}, "renderer_version": T.RENDERER_VERSION, "template_version": T.TEMPLATE_VERSION}
    for fmt, path in files.items():
        out["files"][fmt] = validate_file(path, fmt)
    if preview_pdf:
        out["preview"] = validate_file(preview_pdf, "pdf")
    return out


__all__ = ["ValidationFailed", "sha256_of", "validate_file", "validate_all", "validate_pdf", "validate_docx", "validate_pptx", "validate_xlsx"]
