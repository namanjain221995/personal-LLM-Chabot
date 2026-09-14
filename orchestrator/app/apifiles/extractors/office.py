"""DOCX sections and PPTX slides (design §4.4, 2026-09-13).

BOTH ARE ZIPS, SO BOTH PASS THE BOMB CAPS FIRST. `core/archive.check_zip_
container` applies the chat app's four independent budgets (total uncompressed
ARCHIVE_MAX_UNCOMPRESSED_MB 2,048, member count, per-member ratio 200, unsafe
names) from the central directory before any reader opens a member. A refusal
there is `file_too_complex` — the file is a decompression bomb or larger than
the ceiling — and a zip that does not even parse is `file_corrupt`. RLIMIT_AS
and RLIMIT_FSIZE in the child are the second line: the header can lie.

DOCX goes through `core.docx.extract_docx_text` — the chat app's reader,
imported — with `max_chars` from the spec rather than chat's 400,000 (an API
document is read whole). DOCX has no pages, so the text is cut into sections
of ≤ 3,000 characters at paragraph boundaries and citations say `§n`. Known
gap (design R14, not fixed this wave): headers, footers, footnotes and images.

PPTX: python-pptx, per slide, shape text frames in z-order, table cells
tab-joined, then `Notes:` and the notes text. `page` = slide number. Measured
for the design: 300 slides + notes extracted in 0.08 s.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List

from . import FileCorrupt, FileTooComplex, PagesWriter, SECTION_CHARS, Spec, check_source_size, split_sections

#: The docx reader's character bound when the parent sends none: 1 GiB of
#: characters is far past the 50,000-chunk index ceiling (75 M characters),
#: so this only exists to keep a pathological document finite.
DEFAULT_MAX_CHARS = 200_000_000


def check_container(spec: Spec, label: str) -> None:
    """The zip budgets, size ceiling first (cheapest)."""
    from ...core import archive

    check_source_size(spec, 512 * 1024 * 1024)
    try:
        archive.check_zip_container(spec.source, label=label)
    except archive.ArchiveError as exc:
        # Two ArchiveError families: "not a readable ZIP" (damaged) and the
        # budget refusals (bomb / too big / too many entries). The sentence we
        # return is ours either way; the exception text is never echoed.
        if "not a readable" in str(exc):
            raise FileCorrupt() from None
        raise FileTooComplex("it expands beyond the decompression ceiling") from None


def extract_docx(spec: Spec) -> Dict[str, Any]:
    from ...core.docx import DocxError, extract_docx_text

    check_container(spec, "document")
    with open(spec.source, "rb") as fh:
        data = fh.read()
    try:
        text = extract_docx_text(data, max_chars=int(spec.cap("chars", DEFAULT_MAX_CHARS)))
    except DocxError:
        raise FileCorrupt() from None
    del data
    paragraphs = (p.strip() for p in text.split("\n"))
    with PagesWriter(spec.derived_dir) as pages:
        for number, section in enumerate(split_sections(paragraphs, max_chars=SECTION_CHARS), start=1):
            pages.add(number, section)
    return {
        "sections": pages.pages,
        "chars": pages.chars,
        "estimated_tokens": pages.estimated_tokens,
        "unit": "section",
    }


def _shape_texts(shape) -> Iterator[str]:
    """Text of one shape, recursing into groups; tables tab-joined per row."""
    if getattr(shape, "shape_type", None) == 6 or hasattr(shape, "shapes"):  # MSO_SHAPE_TYPE.GROUP
        for child in getattr(shape, "shapes", []) or []:
            yield from _shape_texts(child)
        return
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            line = "\t".join(cells).strip()
            if line:
                yield line
        return
    if getattr(shape, "has_text_frame", False):
        text = shape.text_frame.text.strip()
        if text:
            yield text


def extract_pptx(spec: Spec) -> Dict[str, Any]:
    check_container(spec, "presentation")
    try:
        from pptx import Presentation

        deck = Presentation(spec.source)
    except Exception:  # noqa: BLE001 — python-pptx raises many types for a bad package
        raise FileCorrupt() from None
    with PagesWriter(spec.derived_dir) as pages:
        for number, slide in enumerate(deck.slides, start=1):
            parts: List[str] = []
            # `slide.shapes` iterates in z-order (the XML spTree order).
            for shape in slide.shapes:
                parts.extend(_shape_texts(shape))
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip() if slide.notes_slide.notes_text_frame else ""
                if notes:
                    parts.append("Notes: " + notes)
            pages.add(number, "\n".join(parts))
    return {
        "slides": pages.pages,
        "chars": pages.chars,
        "estimated_tokens": pages.estimated_tokens,
        "unit": "slide",
    }
