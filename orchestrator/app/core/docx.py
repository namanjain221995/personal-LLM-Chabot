"""DOCX text extraction with the standard library only (2026-08-07).

A .docx is a zip whose word/document.xml holds every paragraph and table.
Parsing it with zipfile + ElementTree avoids a new dependency (and the
aarch64 wheel question entirely) while capturing what matters for Q&A:
paragraph text in order, tables as tab-separated rows, and page-break-ish
structure. Fidelity extras (footnotes, headers, images) are deliberately
out of scope — this feeds a language model, not a renderer.
"""
from __future__ import annotations

import contextlib
import io
import re
import zipfile
from xml.etree import ElementTree

from .archive import open_zip

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Parts of a .docx whose directory entries are parsed (a real one lists tens):
#: a crafted one listing millions was parsed whole, ~500 B of memory each
#: (archive.open_zip, 2026-10-03).
_MAX_PARTS = 10_000


class DocxError(RuntimeError):
    """Not a readable .docx file."""


def is_docx(data: bytes) -> bool:
    """True when the bytes are a zip that contains a Word document part."""
    if not data.startswith(b"PK"):
        return False
    try:
        with open_zip(io.BytesIO(data), _MAX_PARTS) as zf:
            return "word/document.xml" in zf.namelist()
    except Exception:
        return False


def is_docx_file(path: str) -> bool:
    """`is_docx` for a file on disk, never read whole."""
    try:
        with open(path, "rb") as fh, open_zip(fh, _MAX_PARTS) as zf:
            return "word/document.xml" in zf.namelist()
    except Exception:
        return False


def _cell_text(cell) -> str:
    return " ".join(
        t.text for t in cell.iter(f"{_W}t") if t.text
    ).strip()


def extract_docx_text(data: bytes, max_chars: int = 400_000) -> str:
    """Paragraphs in order; tables as one tab-separated line per row."""
    try:
        with open_zip(io.BytesIO(data), _MAX_PARTS) as zf:
            xml = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocxError("not a readable .docx file") from exc
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DocxError("the .docx document XML is malformed") from exc

    body = root.find(f"{_W}body")
    if body is None:
        return ""
    blocks: list[str] = []
    used = 0
    for child in body:
        if used >= max_chars:
            break
        for line in _block_lines(child):
            blocks.append(line)
            used += len(line)
    return _joined(blocks, max_chars)


def _block_lines(child) -> list[str]:
    """The text lines of one body child: a paragraph is one line, a table
    one tab-separated line per row; anything else none."""
    if child.tag == f"{_W}p":
        text = "".join(t.text for t in child.iter(f"{_W}t") if t.text).strip()
        return [text] if text else []
    if child.tag == f"{_W}tbl":
        lines = []
        for row in child.iter(f"{_W}tr"):
            line = "\t".join(_cell_text(c) for c in row.iter(f"{_W}tc")).strip()
            if line:
                lines.append(line)
        return lines
    return []


def _joined(blocks: list[str], max_chars: int) -> str:
    text = "\n".join(blocks)[:max_chars]
    # Word documents love non-breaking spaces; normalize for the model.
    return re.sub(r"\xa0", " ", text)


#: Bytes of word/document.xml fed to the streaming parser at a time.
_XML_CHUNK = 1 << 20


def extract_docx_file(path: str, max_chars: int = 400_000) -> tuple[str, bool]:
    """`extract_docx_text` for a .docx ON DISK, never read whole: the zip's
    directory is read, then word/document.xml is streamed through a pull
    parser and parsing STOPS once `max_chars` of text is in hand. Memory is
    one paragraph or table at a time, whatever the file's size (2026-10-03,
    docs/chat-media/LIMITS.md). -> (text, whether the whole body was read)."""
    opened = contextlib.ExitStack()
    try:
        zf = open_zip(opened.enter_context(open(path, "rb")), _MAX_PARTS)
    except (zipfile.BadZipFile, OSError) as exc:
        opened.close()
        raise DocxError("not a readable .docx file") from exc
    blocks: list[str] = []
    used = 0
    skipped = False
    with opened, zf:
        try:
            src = zf.open("word/document.xml")
        except KeyError as exc:
            raise DocxError("not a readable .docx file") from exc
        parser = ElementTree.XMLPullParser(events=("start", "end"))
        depth = 0
        body = None
        with src:
            while used < max_chars:
                chunk = src.read(_XML_CHUNK)
                if not chunk:
                    return _joined(blocks, max_chars), not skipped and used <= max_chars
                try:
                    parser.feed(chunk)
                    events = list(parser.read_events())
                except ElementTree.ParseError as exc:
                    if not blocks:
                        raise DocxError("the .docx document XML is malformed") from exc
                    break
                for event, elem in events:
                    if event == "start":
                        depth += 1
                        if depth == 2 and elem.tag == f"{_W}body":
                            body = elem
                        continue
                    depth -= 1
                    if depth == 2 and body is not None:
                        # A whole body child: read it, then let it go.
                        lines = _block_lines(elem)
                        if used < max_chars:
                            for line in lines:
                                blocks.append(line)
                                used += len(line)
                        elif lines:
                            skipped = True
                        body.remove(elem)
    # Stopped at the budget, with more of the file unread.
    return _joined(blocks, max_chars), False
