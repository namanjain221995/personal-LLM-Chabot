"""HTML → PDF with WeasyPrint, in-process, with a fetcher that fetches nothing.

WHY IN-PROCESS AND NOT PANDOC. The legacy report path shells out to pandoc,
which hands WeasyPrint HTML it never styled and relies on a regex to strip
anything fetchable (core/report_render.py, where the 2026-09-04 loopback
probe is recorded: three references in a report became three GET requests
from inside the container). Here the HTML is OURS — html.py escapes every
model string and references charts by bare filename — and the URL fetcher
below is the second, independent gate: it resolves a bare filename inside
the assets directory and raises for every other shape, so no http, https,
file, data, protocol-relative, absolute or traversing reference can produce
a fetch even if the first gate were bypassed. `test_artifact_render_pdf.py`
proves it with a listening socket that must receive nothing.

RESOURCE BOUNDS. WeasyPrint runs in the caller's process; the caller (the
render worker) is a subprocess with RLIMIT_AS and RLIMIT_CPU, and the job
runner kills it on the wall-clock timeout. What this module enforces is the
PAGE CAP: a document over types.MAX_PAGES is a page-count bomb and is
refused after the count, with a sentence, not shipped.

MEASURED (2026-09-11, aarch64 host, WeasyPrint 69 — 70 renders the same
suites within the same bounds, in
test_artifact_render_version.py): an empty page renders in ~0.06 s; a
10-section executive report (two charts, three tables, cover, contents) to
PDF + DOCX in ~0.8 s; a 12-slide deck to PPTX + its 13-page preview PDF in
~0.5 s; a 3-sheet workbook plus summary PDF in ~0.2 s; the worker subprocess
end to end, interpreter start included, in ~0.7-1.5 s. The 180 s render
timeout (config.artifact_render_timeout_s) is two orders of magnitude above
that, which is the point: it is there for a page-count bomb, not for a slow
document.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from .. import types as T

#: MIME types the fetcher will hand back. A chart is a PNG; nothing else is
#: ever referenced.
_ALLOWED_SUFFIXES = {".png": "image/png"}


class RefusedFetch(ValueError):
    """The renderer asked for a resource it may not have."""


class AssetFetcher:
    """A WeasyPrint `url_fetcher` that serves ONLY `<assets_dir>/<bare name>.png`.

    WeasyPrint resolves every reference against `base_url` before calling the
    fetcher, so a bare `chart-1.png` arrives as `file:///<assets_dir>/chart-1.png`.
    Anything else — a different scheme, a different directory, a traversal
    that normalises elsewhere, a symlink out of the directory — is refused.

    WHY AN OBJECT AND NOT A FUNCTION. WeasyPrint 70 (the version the image
    resolves `weasyprint>=61` to since 2026-09-11) changed the fetcher
    contract in two ways a plain function cannot meet: the result must be a
    `weasyprint.urls.URLFetcherResponse` (a dict was deprecated in 69 and is
    an AssertionError in 70), and on a fetcher error it reads
    `url_fetcher._fail_on_errors` with no default — so a refusal, the very
    thing this fetcher exists to do, raised AttributeError out of the render
    instead of the logged URLFetchingError. The e2e smoke of 2026-09-11 found
    it: every deck PDF failed in the container while the venv (69) passed.
    """

    #: Read by WeasyPrint when the fetcher raises. False: the refusal is a
    #: logged URLFetchingError and a gap in the page, never a failed render.
    _fail_on_errors = False

    def __init__(self, assets_dir: str | Path) -> None:
        self.root = Path(assets_dir).resolve()

    def __call__(self, url: str, *args: Any, **kwargs: Any) -> Any:
        return self.fetch(url)

    def resolve(self, url: str) -> Path:
        """The file `url` may be served from, or RefusedFetch."""
        root = self.root
        parts = urlsplit(url)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            raise RefusedFetch(f"refused {parts.scheme or 'relative'} reference")
        path = Path(unquote(parts.path))
        if not path.is_absolute():
            raise RefusedFetch("refused relative reference")
        if path.parent != root:
            # Compare the UNRESOLVED parent first: `../x.png` normalises to a
            # sibling directory and fails here; a symlink inside root that
            # points elsewhere fails the resolved check below.
            raise RefusedFetch("refused reference outside the assets directory")
        suffix = path.suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            raise RefusedFetch("refused non-image reference")
        real = path.resolve()
        if real.parent != root or not real.is_file():
            raise RefusedFetch("refused reference outside the assets directory")
        return real

    def fetch(self, url: str) -> Any:
        real = self.resolve(url)
        with open(real, "rb") as fh:
            data = fh.read()
        return _response(url, data, _ALLOWED_SUFFIXES[real.suffix.lower()])


def _response(url: str, data: bytes, mime_type: str) -> Any:
    """What the installed WeasyPrint wants back from a fetcher: its
    URLFetcherResponse (69+), or the dict older releases understood."""
    try:
        from weasyprint.urls import URLFetcherResponse  # lazy: native libs
    except ImportError:  # pragma: no cover - weasyprint < 69, or not installed (the unit tests)
        return {"string": data, "mime_type": mime_type, "redirected_url": url}
    return URLFetcherResponse(url, body=data, headers={"Content-Type": mime_type})


def make_url_fetcher(assets_dir: str | Path) -> Callable[..., Any]:
    """The fetcher `render_html_pdf` hands WeasyPrint; see AssetFetcher."""
    return AssetFetcher(assets_dir)


def render_html_pdf(html: str, out_path: str | Path, assets_dir: str | Path, *, max_pages: int = T.MAX_PAGES) -> int:
    """Write `html` as a PDF at `out_path`; return the page count.

    A RefusedFetch from the fetcher never reaches the caller: WeasyPrint
    catches fetcher errors, logs them and continues without the resource, so
    a refused reference is a visible gap in the page, not a crash. Raises
    ValueError when the PDF exceeds `max_pages` or came out empty; the file is
    removed first so nothing over the cap is ever left on disk.
    """
    from weasyprint import HTML  # lazy: native libs; tests/test_imports.py bans eager import

    assets = Path(assets_dir).resolve()
    out = Path(out_path)
    document = HTML(string=html, base_url=str(assets) + os.sep, url_fetcher=make_url_fetcher(assets))
    document.write_pdf(target=str(out))
    pages = pdf_page_count(out)
    if pages <= 0:
        out.unlink(missing_ok=True)
        raise ValueError("the PDF came out empty")
    if pages > max_pages:
        out.unlink(missing_ok=True)
        raise ValueError(f"the document came to {pages} pages; the ceiling is {max_pages}")
    return pages


def pdf_page_count(path: str | Path) -> int:
    import pypdfium2 as pdfium  # lazy: arm64 wheel, no system deps

    pdf = pdfium.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


__all__ = ["RefusedFetch", "make_url_fetcher", "render_html_pdf", "pdf_page_count"]
