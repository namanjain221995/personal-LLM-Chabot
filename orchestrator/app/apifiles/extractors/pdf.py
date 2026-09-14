"""PDF text layer, page by page, from the PATH (design §4.4, 2026-09-13).

WHY NOT `core/pdf.extract_pdf_pages`. That function takes the whole PDF as a
base64 string, decodes it in memory, holds the process-wide PDFIUM_LOCK and
stops silently at 400,000 characters (~page 227 of the measured 1,756-char
report). For a 1 GiB, 10,000-page API document every one of those is wrong:
PDFium reads lazily from a path, this child has its own PDFium (so the chat
app's previews never queue behind it), and every page is recorded. Measured
2026-09-13: the 1,000-page test fixture (978 KB, 2,335,801 characters) was read
into pages.jsonl in 0.88-1.11 s INCLUDING the child's interpreter start.

WHAT A "THIN" PAGE IS. Fewer than `engines.document.TEXT_OK_CHARS` (200)
characters of text layer — the chat app's own threshold for "render this page
and OCR it". The parent passes the number in the spec so this child imports
nothing from `engines/` (which pulls in the LLM client). Thin pages are listed
in `thin_pages.json` for the `ocr` stage (`apifiles/ocr_pages.py`).

A PASSWORD-PROTECTED OR MALFORMED PDF raises PdfiumError on open → the closed
`file_corrupt` verdict. The exception text is never echoed.
"""
from __future__ import annotations

from typing import Any, Dict

from . import FileCorrupt, FileTooComplex, PagesWriter, Spec, THIN_PAGES_NAME, atomic_write_json, check_source_size

#: `engines.document.TEXT_OK_CHARS` at the time of writing; the parent sends
#: the live value in `caps.thin_page_chars`, this is only the fallback.
DEFAULT_THIN_PAGE_CHARS = 200


def _words(n: int) -> str:
    return f"{n:,}"


#: PUBLIC_API_FILES_PDF_MAX_BYTES when the parent sends no cap (limits.py).
DEFAULT_MAX_BYTES = 1 << 30


def open_document(path: str, spec: "Spec | None" = None):
    """A PdfDocument opened from the path, or FileCorrupt.

    With `spec`, the byte ceiling is checked FIRST (review finding,
    2026-09-13: it was never enforced for PDFs). PDFium rebuilds a damaged
    xref by scanning the whole file, so a 100 GiB "PDF" would otherwise hold
    a CPU slot until the address-space or CPU ceiling killed it, instead of
    getting the immediate "larger than 1,024 MiB" verdict."""
    import pypdfium2 as pdfium  # lazy: the arm64 wheel, and only in the child

    if spec is not None:
        check_source_size(spec, DEFAULT_MAX_BYTES)

    try:
        return pdfium.PdfDocument(path)
    except Exception:  # noqa: BLE001 — PdfiumError for encrypted/damaged; never echoed
        raise FileCorrupt() from None


def extract(spec: Spec) -> Dict[str, Any]:
    """pages.jsonl + thin_pages.json; facts pages, text_pages, chars,
    estimated_tokens, thin_pages."""
    max_pages = int(spec.cap("pages", 10_000))
    thin_below = int(spec.cap("thin_page_chars", DEFAULT_THIN_PAGE_CHARS))
    pdf = open_document(spec.source, spec)
    try:
        total = len(pdf)
        if total > max_pages:
            raise FileTooComplex(f"more than {_words(max_pages)} pages")
        with PagesWriter(spec.derived_dir, thin_below=thin_below) as pages:
            for index in range(total):
                try:
                    page = pdf[index]
                    textpage = page.get_textpage()
                    text = (textpage.get_text_range() or "").strip()
                    textpage.close()
                    page.close()
                except Exception:  # noqa: BLE001 — one unreadable page object
                    # A page PDFium cannot parse is recorded as having no text
                    # layer, which makes it an OCR candidate — the same outcome
                    # a scanned page gets — instead of failing the whole file.
                    text = ""
                # PDFium hands back CRLF line ends and NULs from some producers;
                # neither helps a model and NUL breaks a jsonb round trip.
                text = text.replace("\r\n", "\n").replace("\x00", "")
                pages.add(index + 1, text, source="text")
        atomic_write_json(f"{spec.derived_dir}/{THIN_PAGES_NAME}", pages.thin)
        return {
            "pages": total,
            "text_pages": pages.text_pages,
            "chars": pages.chars,
            "estimated_tokens": pages.estimated_tokens,
            "thin_pages": len(pages.thin),
            "unit": "page",
        }
    finally:
        pdf.close()
