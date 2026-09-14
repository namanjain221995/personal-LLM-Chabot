"""Plain text, Markdown, code, JSON documents and HTML (design §4.4).

TEXT is decoded as a STREAM (UTF-8 with errors=replace, or UTF-16 when the
file starts with a BOM) in 1 MiB reads and cut into sections of ≤ 3,000
characters at line boundaries. The design ceiling for a text file is 1 GiB;
reading it whole would put 1 GiB of bytes plus ~4 GiB of str objects into the
child for nothing.

A JSON OBJECT (a config, an OpenAPI spec) is kind `text` (design finding #9,
measured: a DuckDB profile of one struct row is useless). When it parses
within the pretty-print ceiling it is re-indented so sections break at
keys; otherwise it is read as the raw text it is.

HTML ≤ PUBLIC_API_FILES_HTML_READABLE_MAX_BYTES (64 MiB) goes through the chat
app's `core.extract.extract_readable` (trafilatura, plus the structured and
embedded-record passes a fetched page gets); a larger document falls back to
`core.extract._strip_tags` over 1 MiB windows, because trafilatura parses the
whole DOM in memory.
"""
from __future__ import annotations

import codecs
import json
from typing import Any, Dict, Iterator, List

from . import FileCorrupt, PagesWriter, SECTION_CHARS, Spec, check_source_size, source_suffix, split_sections

_READ = 1024 * 1024

#: A JSON document above this is not pretty-printed (json.load of 64 MiB is
#: ~0.5 GiB of Python objects); it is sectioned as the raw text instead.
JSON_PRETTY_MAX_BYTES = 64 * 1024 * 1024


def _decoder_for(head: bytes):
    if head.startswith(codecs.BOM_UTF8):
        return codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return codecs.getincrementaldecoder("utf-16")(errors="replace")
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


#: A "line" longer than this many characters is yielded in windows cut at a
#: space (review finding, 2026-09-13): a 1 GiB one-line file must not become
#: one 1-4 GiB str under RLIMIT_AS, nor be re-scanned per 1 MiB read. The cut
#: turns one space every ~1 MiB into a line break, which sectioning absorbs.
LINE_WINDOW_CHARS = 1 << 20


def iter_lines(path: str) -> Iterator[str]:
    """Decoded lines, streamed; a final line without a newline is yielded.
    Linear in the file: only newly decoded text is searched for a newline,
    and a line without one is emitted in LINE_WINDOW_CHARS windows."""

    def clean(line: str) -> str:
        return line.rstrip("\r").replace("\x00", "")

    with open(path, "rb") as fh:
        head = fh.read(4)
        decoder = _decoder_for(head)
        pending: List[str] = []
        pending_len = 0
        chunk = head
        while True:
            text = decoder.decode(chunk, final=not chunk)
            if "\n" in text:
                parts = text.split("\n")
                pending.append(parts[0])
                yield clean("".join(pending))
                for line in parts[1:-1]:
                    yield clean(line)
                pending, pending_len = [parts[-1]], len(parts[-1])
            elif text:
                pending.append(text)
                pending_len += len(text)
            while pending_len > LINE_WINDOW_CHARS:
                joined = "".join(pending)
                cut = joined.rfind(" ", LINE_WINDOW_CHARS // 2, LINE_WINDOW_CHARS)
                cut = cut if cut > 0 else LINE_WINDOW_CHARS
                yield clean(joined[:cut])
                rest = joined[cut:]
                pending, pending_len = [rest], len(rest)
            if not chunk:
                break
            chunk = fh.read(_READ)
    tail = "".join(pending)
    if tail:
        yield clean(tail)


def _check_size(spec: Spec) -> int:
    return check_source_size(spec, 1 << 30, what="of text")


def _write_sections(spec: Spec, blocks: Iterator[str]) -> Dict[str, Any]:
    with PagesWriter(spec.derived_dir) as pages:
        for number, section in enumerate(split_sections(blocks, max_chars=SECTION_CHARS), start=1):
            pages.add(number, section)
    return {
        "sections": pages.pages,
        "chars": pages.chars,
        "estimated_tokens": pages.estimated_tokens,
        "unit": "section",
    }


def _pretty_json_lines(path: str) -> Iterator[str]:
    with open(path, "rb") as fh:
        head = fh.read(4)
        fh.seek(0)
        raw = fh.read()
    encoding = "utf-16" if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    document = json.loads(raw.decode(encoding, errors="replace"))
    yield from json.dumps(document, indent=2, ensure_ascii=False).split("\n")


def extract_text(spec: Spec) -> Dict[str, Any]:
    size = _check_size(spec)
    if source_suffix(spec.source) == ".json" and size <= JSON_PRETTY_MAX_BYTES:
        try:
            return _write_sections(spec, _pretty_json_lines(spec.source))
        except (ValueError, RecursionError):
            pass  # not valid JSON after all: read it as the text it is
    return _write_sections(spec, iter_lines(spec.source))


def _strip_windows(path: str) -> Iterator[str]:
    """Tag-stripped text of an HTML file too large for trafilatura, 1 MiB at a
    time. A tag cut at a window edge costs a few stray characters, not a crash."""
    from ...core.extract import _strip_tags

    with open(path, "rb") as fh:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = fh.read(_READ)
            if not chunk:
                break
            text = _strip_tags(decoder.decode(chunk))
            yield from (line.strip() for line in text.split("\n"))


def extract_html(spec: Spec) -> Dict[str, Any]:
    size = _check_size(spec)
    readable_max = int(spec.cap("html_readable_bytes", 64 * 1024 * 1024))
    if size > readable_max:
        return _write_sections(spec, _strip_windows(spec.source))
    from ...core.extract import UnsupportedContentError, extract_readable

    with open(spec.source, "rb") as fh:
        body = fh.read()
    try:
        extracted = extract_readable("text/html", body, url="")
    except UnsupportedContentError:
        raise FileCorrupt() from None
    del body
    text = extracted.text or ""
    blocks = (line.strip() for line in text.split("\n"))
    facts = _write_sections(spec, blocks)
    return facts
