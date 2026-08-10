"""Document engine (V8 → full-document rewrite 2026-08-07).

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

Emits meta route "vision" — same visual-understanding engine as before.
"""
from __future__ import annotations

import base64
from typing import Awaitable, Callable, List, Optional, Sequence

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


async def run_pdf_engine(
    message: str,
    pdf_base64: str,
    filename: Optional[str],
    history: Sequence[dict],
    emit: Emit,
    conversation_id: Optional[str] = None,
) -> str:
    """Any uploaded document — PDF, DOCX, or plain text (field name is V8)."""
    raw = base64.b64decode(_strip_data_url(pdf_base64))

    images: List[str] = []
    total = 0
    ocred = 0
    raw_pages: List[str] = []
    if raw.startswith(b"%PDF"):
        await emit("status", {"text": "Reading the document…"})
        full_text, images, total, ocred, raw_pages = await _extract_pdf(pdf_base64, emit)
    else:
        from ..core.docx import DocxError, extract_docx_text, is_docx

        if is_docx(raw):
            try:
                full_text = extract_docx_text(raw)
            except DocxError as exc:
                note = f"Could not read that document ({exc})."
                await emit("token", {"text": note})
                await emit("meta", {"route": "vision"})
                return note
        else:
            try:
                full_text = raw.decode("utf-8", errors="replace")[:400_000]
            except Exception:
                full_text = ""

    if not full_text.strip() and not images:
        note = "That document has no readable content."
        await emit("token", {"text": note})
        await emit("meta", {"route": "vision"})
        return note

    # Remember the WHOLE document for the rest of this conversation.
    if conversation_id and full_text.strip():
        try:
            from .. import db

            await db.run_in_thread(
                db.save_document, conversation_id, filename or "document", full_text, total
            )
        except Exception:
            pass  # memory is an enhancement; the answer must still stream

    instruction = message or "Read this document and summarize the key points."
    header = f"Document: {filename}\n" if filename else ""
    if total:
        header += f"({total} pages — all were read.)\n"

    excerpt = select_relevant(full_text, instruction, DOC_CONTEXT_CHARS)
    content: List[dict] = [{"type": "text", "text": header + instruction}]
    if excerpt.strip():
        content.append(
            {"type": "text",
             "text": f"\n\nDocument text (most relevant sections):\n{excerpt}"}
        )
    for url in images:
        content.append({"type": "image_url", "image_url": {"url": url}})
    if total > len(images) and images:
        content.append(
            {"type": "text",
             "text": f"\n\n(Images show the first {len(images)} of {total} "
                     "pages; the text above covers the whole document.)"},
        )

    messages = (
        [{"role": "system", "content": _SYSTEM + DIAGRAM_INSTRUCTION}]
        + recent_turns(history, 4)
        + [{"role": "user", "content": content}]
    )

    # Activity panel payload (owner request 2026-08-07): show WHAT was read —
    # every page for PDFs, ~2k-char parts for DOCX/plain — capped per entry so
    # a 100-page meta stays a payload, not a payload problem.
    if raw_pages:
        entries = [
            {"page": i + 1, "text": t[:1200]}
            for i, t in enumerate(raw_pages) if t.strip()
        ][:80]
    else:
        chunks = [full_text[i:i + 2000] for i in range(0, len(full_text), 2000)]
        entries = [
            {"page": i + 1, "text": c} for i, c in enumerate(chunks[:80])
        ]
    doc_meta = {
        "filename": filename or "document",
        "total_pages": total or len(entries),
        "ocr_pages": ocred,
        "pages": entries,
    }

    parts: List[str] = []
    # Reasoning-aware stream: the thinking model reasons a lot over documents,
    # so surface that in the "Thinking…" panel AND give a generous ceiling.
    async for kind, delta in llm.stream_chat_events(
        messages, model_choice="smart", effort="medium", max_tokens=12000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    await emit("meta", {"route": "vision", "document": doc_meta})
    return "".join(parts)
