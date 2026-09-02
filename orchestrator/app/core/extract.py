"""Readable main-text extraction from fetched pages (Phase 1/2).

Turns a fetched HTML/PDF/plain-text body into clean text for the model,
dropping nav/ads/boilerplate. HTML uses trafilatura (pure-python; lxml has
arm64 wheels); PDF reuses pypdfium2's text layer; other types are refused.

Heavy libs (trafilatura, pypdfium2) import lazily so the app/tests load without
them.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANKS_RE = re.compile(r"\n{3,}")


class UnsupportedContentError(ValueError):
    """The fetched content type can't be read as text (image, binary, …)."""


@dataclass
class Extracted:
    title: str
    text: str
    #: Provenance (2026-09-03), read from the page's own metadata by
    #: core/provenance.page_dates — ISO dates or None, never invented. Every
    #: caller and test that builds Extracted(title=, text=) keeps working.
    published_at: Optional[str] = None
    modified_at: Optional[str] = None
    #: The site's own name (og:site_name / JSON-LD publisher) when it says.
    sitename: str = ""


def _title_from_url(url: str) -> str:
    host = urlparse(url).hostname or url
    return host


def _html_title(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = _TAG_RE.sub("", m.group(1))
    title = _WS_RE.sub(" ", title).strip()
    return title or None


def _strip_tags(html: str) -> str:
    """Last-resort text: drop script/style, strip tags, collapse whitespace."""
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", html)
    text = _WS_RE.sub(" ", text)
    return _BLANKS_RE.sub("\n\n", text).strip()


def _extract_pdf_text(body: bytes) -> str:
    import base64

    from .pdf import render_pdf  # reuse the pypdfium2 text layer

    _images, text, _total = render_pdf(base64.b64encode(body).decode(), max_pages=10)
    return text


def _page_provenance(html: str, url: str, headers: Optional[dict]) -> tuple:
    """(published_iso, modified_iso, sitename) from the page's metadata.

    Bounded and fail-soft: a page that carries no dates yields None, and any
    parser failure yields None — the text extraction above it is never at
    risk. Measured at ~0.02 ms per page for the date pass on this image."""
    published = modified = None
    sitename = ""
    try:
        from . import provenance

        dates = provenance.page_dates(html, url, headers)
        published = dates.published.date().isoformat() if dates.published else None
        modified = dates.modified.date().isoformat() if dates.modified else None
    except Exception:  # noqa: BLE001
        pass
    try:
        import trafilatura

        meta = trafilatura.extract_metadata(html, default_url=url or None)
        sitename = (getattr(meta, "sitename", None) or "").strip()[:120]
    except Exception:  # noqa: BLE001
        pass
    return published, modified, sitename


def extract_readable(
    content_type: str, body: bytes, url: str, headers: Optional[dict] = None
) -> Extracted:
    """Extract (title, readable text) from a fetched body, dispatched by type.

    `headers` (optional) supplies the response's Last-Modified for pages that
    carry no date of their own. Raises UnsupportedContentError for types we
    can't read as text.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    lowered_url = url.lower()

    if "pdf" in ct or lowered_url.endswith(".pdf"):
        modified = None
        try:
            from . import provenance

            dt = provenance.parse_date((headers or {}).get("last-modified"))
            modified = dt.date().isoformat() if dt else None
        except Exception:  # noqa: BLE001
            modified = None
        return Extracted(
            title=_title_from_url(url),
            text=_extract_pdf_text(body),
            modified_at=modified,
        )

    if ct in ("text/plain", "text/markdown"):
        return Extracted(
            title=_title_from_url(url), text=body.decode("utf-8", "replace")
        )

    if "html" in ct or "xml" in ct or ct == "":
        html = body.decode("utf-8", "replace")
        text: Optional[str] = None
        try:
            import trafilatura

            text = trafilatura.extract(
                html, include_comments=False, include_tables=True, favor_recall=True
            )
        except Exception:
            text = None
        if not text:
            text = _strip_tags(html)
        published, modified, sitename = _page_provenance(html, url, headers)
        return Extracted(
            title=_html_title(html) or _title_from_url(url),
            text=text or "",
            published_at=published,
            modified_at=modified,
            sitename=sitename,
        )

    raise UnsupportedContentError(ct or "unknown")


def truncate_chars(text: str, max_chars: int) -> str:
    """Trim to a character budget on a whitespace boundary, with an ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    sp = cut.rfind(" ")
    if sp > max_chars * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + " …"

def extract_readable_and_links(
    content_type: str, body: bytes, url: str, headers: Optional[dict] = None
) -> "tuple[Extracted, list[str]]":
    """One parse pass for the crawler: readable text PLUS harvested links.

    MUST run inside the same single-worker pool as extract_readable —
    trafilatura shares non-thread-safe compiled lxml objects, and a parallel
    parse on another thread can abort the interpreter (see the pool note in
    engines/search.py). Combining the two here means the crawler pays one
    pool submission per page, not two.

    Links come only from HTML (a PDF has none worth walking), are
    absolute-ized against the FINAL post-redirect URL with <base href>
    honoured, and are de-fragmented — fragments multiply a crawl frontier
    with self-links. Measured: 41 ms for ~2,500 links on a 727 KB doc page.
    """
    extracted = extract_readable(content_type, body, url, headers)
    links: list[str] = []
    if "html" in (content_type or "").lower():
        try:
            import lxml.html  # already a trafilatura dependency

            doc = lxml.html.document_fromstring(body)
            doc.make_links_absolute(url, resolve_base_href=True)
            from urllib.parse import urldefrag

            seen: set[str] = set()
            for a in doc.iter("a"):
                href = (a.get("href") or "").strip()
                if not href.startswith(("http://", "https://")):
                    continue
                clean, _frag = urldefrag(href)
                if clean and clean not in seen:
                    seen.add(clean)
                    links.append(clean)
        except Exception:  # noqa: BLE001 — links are an extra, never a gate
            # Loud on purpose: a systematically broken harvest (lxml missing,
            # API change) otherwise looks exactly like "this site has no
            # links", and the crawler quietly explores nothing.
            log.warning("link harvest failed for %s", url, exc_info=True)
            links = []
    return extracted, links

