"""Document engine (V8 → full-document rewrite 2026-08-07; multi-doc 2026-09-02).

The old pipeline read only the FIRST 6 PAGES of any PDF — a 36-page PRD was
answered from a sixth of itself, silently. Now the WHOLE document is read,
ChatGPT-style, and remembered for the rest of the conversation:

  1. Sniff the upload — %PDF → PDF; PK+word/document.xml → DOCX; else text.
  2. PDF: pull the text layer of EVERY page (cheap). Pages whose layer is
     thin (< TEXT_OK_CHARS — scans, photos) are rendered and sent to the
     Unlimited-OCR sidecar, up to OCR_PAGE_BUDGET pages, so a 100-page scan
     still reads; the first pages also go to the model AS IMAGES so layout,
     stamps and signatures stay visible.
  3. The full text (page-marked) is stored in the `documents` table keyed by
     conversation — main.py injects question-relevant excerpts into EVERY
     later turn, so "what did that PDF say about X?" works ten turns later.
  4. The answer prompt gets the question-RELEVANT slice of the document
     (select_relevant), not a blind prefix — the budget goes where the
     question points.

A message may carry SEVERAL documents (up to the composer's cap of five).
Each is extracted and remembered individually; the answer prompt sees one
merged, per-document-labelled text so "compare the two contracts" is a
single question, not five. Page images ride along only for the FIRST PDF —
five documents' worth of page renders would drown the context for no gain.

Emits meta route "vision" — same visual-understanding engine as before.
"""
from __future__ import annotations

import base64
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import llm
from ..config import settings
from ..core.pdf import (MAX_PDF_PAGES, extract_pdf_pages, render_pdf_pages,
                        render_pdf)
from ..core.urls import select_relevant

Emit = Callable[[str, dict], Awaitable[None]]

#: A page with at least this much embedded text is born-digital — its text
#: layer is trusted and the OCR model is not spent on it.
TEXT_OK_CHARS = 200
#: How many thin-text pages may go through the 3.3B OCR sidecar per upload.
OCR_PAGE_BUDGET = 40
#: Per-question char budget for document context in the answer prompt.
DOC_CONTEXT_CHARS = 48_000
#: Documents the ENGINE will merge into one question. A chat request carries
#: at most five references, but one of them may be an ARCHIVE whose expansion
#: legitimately yields more members than that.
MAX_DOCS = 12

_SYSTEM = (
    "You are a careful document analyst. You are given a document's extracted "
    "text (the ENTIRE document was read; the excerpt shown is the part most "
    "relevant to the question) and, for PDFs, images of its first pages. "
    "Answer using what is actually in the document. When asked to extract "
    "fields (invoices, contracts, forms), return the structured values you "
    "find. Do not invent details that are not present."
)


def _strip_data_url(b64: str) -> str:
    return b64.split(",", 1)[-1] if b64.startswith("data:") else b64


def _page_marked(pages: List[str]) -> str:
    return "\n\n".join(
        f"[Page {i + 1}]\n{t}" for i, t in enumerate(pages) if t.strip()
    )


async def _extract_pdf(
    pdf_base64: str, emit: Emit
) -> tuple[str, List[str], int, int, List[str]]:
    """→ (page-marked text, first-page images, total pages, ocr'd pages, pages)."""
    pages, total = extract_pdf_pages(pdf_base64)
    images, _text, _total = render_pdf(pdf_base64)  # first pages, for layout

    ocred = 0
    if settings.ocr_enabled:
        thin = [i for i, t in enumerate(pages) if len(t) < TEXT_OK_CHARS]
        thin = thin[:OCR_PAGE_BUDGET]
        if thin:
            await emit(
                "status",
                {"text": f"Reading {len(thin)} scanned page"
                 f"{'s' if len(thin) != 1 else ''} with OCR…"},
            )
            from .ocr import ocr_images

            page_images = render_pdf_pages(pdf_base64, thin)
            transcripts = await ocr_images(page_images)
            for idx, transcript in zip(thin, transcripts):
                if transcript.strip():
                    ocred += 1
                    pages[idx] = (
                        f"{pages[idx]}\n{transcript}".strip()
                        if pages[idx]
                        else transcript
                    )
    return _page_marked(pages), images, total, ocred, pages


class _Doc:
    """One readable document, extracted."""

    __slots__ = ("name", "full_text", "images", "total", "ocred", "raw_pages")

    def __init__(self, name, full_text, images, total, ocred, raw_pages):
        self.name = name
        self.full_text = full_text
        self.images = images
        self.total = total
        self.ocred = ocred
        self.raw_pages = raw_pages


async def _read_one(
    name: Optional[str], pdf_base64: str, emit: Emit
) -> Tuple[Optional[_Doc], Optional[str]]:
    """Sniff and extract ONE document. → (doc, error note). Exactly one is set."""
    raw = base64.b64decode(_strip_data_url(pdf_base64))
    label = name or "document"

    if raw.startswith(b"%PDF"):
        await emit("status", {"text": f"Reading {label}…"})
        full_text, images, total, ocred, raw_pages = await _extract_pdf(
            pdf_base64, emit
        )
        return _Doc(name, full_text, images, total, ocred, raw_pages), None

    from ..core.docx import DocxError, extract_docx_text, is_docx

    if is_docx(raw):
        try:
            full_text = extract_docx_text(raw)
        except DocxError as exc:
            return None, f"Could not read {label} ({exc})."
    else:
        # A binary that is neither PDF nor DOCX (an executable, a .so, a
        # random blob) must not become 400k characters of mojibake in the
        # prompt — name it honestly instead so the model can SAY what it is.
        head = raw[:8192]
        if b"\x00" in head:
            full_text = (
                f"[Binary file: {label}, {len(raw):,} bytes — contents are "
                "not readable as text.]"
            )
        else:
            try:
                full_text = raw.decode("utf-8", errors="replace")[:400_000]
            except Exception:
                full_text = ""
    return _Doc(name, full_text, [], 0, 0, []), None


async def run_pdf_engine(
    message: str,
    pdf_base64: str,
    filename: Optional[str],
    history: Sequence[dict],
    emit: Emit,
    conversation_id: Optional[str] = None,
    *,
    effort: str = "think",
) -> str:
    """One uploaded document — PDF, DOCX, or plain text (field name is V8)."""
    return await run_pdf_engine_multi(
        message,
        [(filename, pdf_base64)],
        history,
        emit,
        conversation_id=conversation_id,
        effort=effort,
    )


async def run_pdf_engine_multi(
    message: str,
    docs: Sequence[Tuple[Optional[str], str]],
    history: Sequence[dict],
    emit: Emit,
    conversation_id: Optional[str] = None,
    *,
    effort: str = "think",
    extra_images: Optional[Sequence[str]] = None,
) -> str:
    """Up to MAX_DOCS uploaded documents, answered as ONE question.

    `effort` is the composer's Fast/Think/Max, exactly as on the image route
    (2026-08-29). Until then this engine hard-coded ``effort="medium"`` — an
    alias for "think" — so a document uploaded with Fast still ran a full
    reasoning pass, and `meta.effort` (which reports `request.effort` for the
    shared ``route="vision"``) described a level the answer had not run at.
    """
    docs = list(docs)[:MAX_DOCS]
    read: List[_Doc] = []
    failures: List[str] = []
    for name, b64 in docs:
        doc, err = await _read_one(name, b64, emit)
        if doc is not None and (doc.full_text.strip() or doc.images):
            read.append(doc)
        elif err:
            failures.append(err)
        else:
            failures.append(f"{name or 'A document'} has no readable content.")

    if not read:
        note = failures[0] if failures else "That document has no readable content."
        await emit("token", {"text": note})
        await emit("meta", {"route": "vision"})
        return note

    # Remember WHOLE documents for the rest of this conversation — each under
    # its own name, so later questions can pull excerpts from any of them.
    if conversation_id:
        for doc in read:
            if not doc.full_text.strip():
                continue
            try:
                from .. import db

                await db.run_in_thread(
                    db.save_document,
                    conversation_id,
                    doc.name or "document",
                    doc.full_text,
                    doc.total,
                )
            except Exception:
                pass  # memory is an enhancement; the answer must still stream

    instruction = message or "Read this document and summarize the key points."

    if len(read) == 1:
        # Single document: byte-for-byte the header the engine always built,
        # so nothing downstream (or in anyone's habits) shifts.
        doc = read[0]
        header = f"Document: {doc.name}\n" if doc.name else ""
        if doc.total:
            header += f"({doc.total} pages — all were read.)\n"
        merged = doc.full_text
        images = doc.images
        image_owner = doc
    else:
        lines = [f"{len(read)} documents were uploaded and ALL were read:"]
        for i, doc in enumerate(read, 1):
            pages = f" ({doc.total} pages)" if doc.total else ""
            lines.append(f"  {i}. {doc.name or f'document {i}'}{pages}")
        header = "\n".join(lines) + "\n"
        merged = "\n\n".join(
            f"===== Document {i}: {doc.name or f'document {i}'} =====\n{doc.full_text}"
            for i, doc in enumerate(read, 1)
        )
        # Page images from the FIRST PDF only; five documents of renders
        # would drown the context for no gain.
        image_owner = next((d for d in read if d.images), None)
        images = image_owner.images if image_owner else []
    if failures:
        header += "".join(f"(Note: {f})\n" for f in failures[:3])

    excerpt = select_relevant(merged, instruction, DOC_CONTEXT_CHARS)
    content: List[dict] = [{"type": "text", "text": header + instruction}]
    if excerpt.strip():
        content.append(
            {"type": "text",
             "text": f"\n\nDocument text (most relevant sections):\n{excerpt}"}
        )
    for url in images:
        content.append({"type": "image_url", "image_url": {"url": url}})
    # Images found INSIDE an uploaded archive (data: URLs, already capped by
    # the expander) — the model sees them exactly like attached images.
    for url in extra_images or []:
        content.append({"type": "image_url", "image_url": {"url": url}})
    if image_owner is not None and image_owner.total > len(images) and images:
        owner_note = (
            f"\n\n(Images show the first {len(images)} of {image_owner.total} "
            "pages; the text above covers the whole document.)"
            if len(read) == 1
            else f"\n\n(Images show the first {len(images)} pages of "
            f"{image_owner.name or 'the first PDF'} only; the text above "
            "covers every document in full.)"
        )
        content.append({"type": "text", "text": owner_note})

    messages = (
        [{"role": "system", "content": _SYSTEM + DIAGRAM_INSTRUCTION}]
        + recent_turns(history, 4)
        + [{"role": "user", "content": content}]
    )

    # Activity panel payload (owner request 2026-08-07): show WHAT was read —
    # every page for PDFs, ~2k-char parts for DOCX/plain — capped per entry so
    # a 100-page meta stays a payload, not a payload problem. With several
    # documents the 80-entry budget is shared in upload order, each entry
    # prefixed with its document's name.
    entries: List[dict] = []
    for doc in read:
        prefix = f"[{doc.name}] " if len(read) > 1 and doc.name else ""
        if doc.raw_pages:
            for i, t in enumerate(doc.raw_pages):
                if t.strip():
                    entries.append({"page": i + 1, "text": prefix + t[:1200]})
        else:
            chunks = [
                doc.full_text[i:i + 2000]
                for i in range(0, len(doc.full_text), 2000)
            ]
            for i, c in enumerate(chunks):
                entries.append({"page": i + 1, "text": prefix + c})
        if len(entries) >= 80:
            break
    entries = entries[:80]
    first = read[0]
    doc_meta = {
        "filename": (
            first.name or "document"
            if len(read) == 1
            else f"{first.name or 'document'} (+{len(read) - 1} more)"
        ),
        "total_pages": sum(d.total for d in read) or len(entries),
        "ocr_pages": sum(d.ocred for d in read),
        "pages": entries,
    }

    parts: List[str] = []
    # Reasoning-aware stream: the thinking model reasons a lot over documents,
    # so surface that in the "Thinking…" panel AND give a generous ceiling.
    async for kind, delta in llm.stream_chat_events(
        messages,
        model_choice="smart",
        effort=llm.normalize_effort(effort),
        max_tokens=12000,
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    await emit("meta", {"route": "vision", "document": doc_meta})
    return "".join(parts)
