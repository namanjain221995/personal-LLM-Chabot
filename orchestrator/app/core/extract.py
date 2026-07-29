"""Readable main-text extraction from fetched pages (Phase 1/2).

Turns a fetched HTML/PDF/plain-text body into clean text for the model,
dropping nav/ads/boilerplate. HTML uses trafilatura (pure-python; lxml has
arm64 wheels); PDF reuses pypdfium2's text layer; other types are refused.

Heavy libs (trafilatura, pypdfium2) import lazily so the app/tests load without
them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

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


def extract_readable(content_type: str, body: bytes, url: str) -> Extracted:
    """Extract (title, readable text) from a fetched body, dispatched by type.

    Raises UnsupportedContentError for types we can't read as text.
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    lowered_url = url.lower()

    if "pdf" in ct or lowered_url.endswith(".pdf"):
        return Extracted(title=_title_from_url(url), text=_extract_pdf_text(body))

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
        return Extracted(
            title=_html_title(html) or _title_from_url(url), text=text or ""
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
