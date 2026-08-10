"""DOCX text extraction with the standard library only (2026-08-07).

A .docx is a zip whose word/document.xml holds every paragraph and table.
Parsing it with zipfile + ElementTree avoids a new dependency (and the
aarch64 wheel question entirely) while capturing what matters for Q&A:
paragraph text in order, tables as tab-separated rows, and page-break-ish
structure. Fidelity extras (footnotes, headers, images) are deliberately
out of scope — this feeds a language model, not a renderer.
"""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocxError(RuntimeError):
    """Not a readable .docx file."""


def is_docx(data: bytes) -> bool:
    """True when the bytes are a zip that contains a Word document part."""
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
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
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
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
        if child.tag == f"{_W}p":
            text = "".join(t.text for t in child.iter(f"{_W}t") if t.text).strip()
            if text:
                blocks.append(text)
                used += len(text)
        elif child.tag == f"{_W}tbl":
            for row in child.iter(f"{_W}tr"):
                cells = [_cell_text(c) for c in row.iter(f"{_W}tc")]
                line = "\t".join(cells).strip()
                if line:
                    blocks.append(line)
                    used += len(line)
    text = "\n".join(blocks)[:max_chars]
    # Word documents love non-breaking spaces; normalize for the model.
    return re.sub(r"\xa0", " ", text)
