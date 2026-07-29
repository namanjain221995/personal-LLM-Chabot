"""Document (PDF) engine (V8, 2026-07-23).

Renders an uploaded PDF to page images + text (core/pdf.py) and sends both to
the multimodal main model, so the user can ask about a PDF just like an image.
Emits meta route "vision" — it is the same visual-understanding engine.
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, Sequence

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import llm
from ..core.pdf import render_pdf

Emit = Callable[[str, dict], Awaitable[None]]

_SYSTEM = (
    "You are a careful document analyst. You are given a PDF as page images "
    "plus its extracted text. Answer the user's question using what is actually "
    "in the document. When asked to extract fields (invoices, contracts, forms), "
    "return the structured values you find. Do not invent details that are not "
    "present."
)


async def run_pdf_engine(
    message: str,
    pdf_base64: str,
    filename: Optional[str],
    history: Sequence[dict],
    emit: Emit,
) -> str:
    images, text, total = render_pdf(pdf_base64)
    if not images:
        note = "That PDF has no readable pages."
        await emit("token", {"text": note})
        await emit("meta", {"route": "vision"})
        return note

    shown = len(images)
    instruction = message or "Read this document and summarize the key points."
    header = f'Document: {filename}\n\n' if filename else ""

    content: List[dict] = [{"type": "text", "text": header + instruction}]
    if text.strip():
        content.append(
            {"type": "text", "text": f"\n\nExtracted text:\n{text}"}
        )
    for url in images:
        content.append({"type": "image_url", "image_url": {"url": url}})
    if total > shown:
        content.append(
            {
                "type": "text",
                "text": f"\n\n(Only the first {shown} of {total} pages are shown.)",
            }
        )

    messages = (
        [{"role": "system", "content": _SYSTEM + DIAGRAM_INSTRUCTION}]
        + recent_turns(history, 4)
        + [{"role": "user", "content": content}]
    )

    parts: List[str] = []
    # Reasoning-aware stream: the thinking model reasons a lot over multi-page
    # images, so surface that in the "Thinking…" panel AND give a generous
    # ceiling — otherwise the budget is spent invisibly and the answer is empty.
    async for kind, delta in llm.stream_chat_events(
        messages, model_choice="smart", effort="medium", max_tokens=12000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    await emit("meta", {"route": "vision"})
    return "".join(parts)
