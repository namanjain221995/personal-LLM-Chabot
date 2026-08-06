"""Vision engine (spec §8, vLLM design).

Qwen3-VL-4B-Instruct on the vLLM vision endpoint (OpenAI-compatible) with the
image sent as multimodal content: [{"type": "text", ...}, {"type":
"image_url", "image_url": {"url": "data:image/png;base64,<b64>"}}]. For
invoices/contracts the model is instructed to lead with a fenced JSON block
of structured fields followed by prose; the structured fields reach the user
inside the streamed answer text. The final meta is the §10 contract shape:
{"route": "vision"}.
"""
from __future__ import annotations

import json
import re
from typing import Awaitable, Callable, List, Optional, Sequence

from .. import llm

Emit = Callable[[str, dict], Awaitable[None]]

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_DATA_URL_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.I)

_SYSTEM = (
    "You are a visual analyst for business documents and images.\n"
    "If the image is an INVOICE: first output a fenced ```json block with "
    "keys vendor, invoice_number, invoice_date, due_date, currency, subtotal, "
    "tax, total, line_items (array of {description, quantity, unit_price, "
    "amount}); then a short prose summary.\n"
    "If the image is a CONTRACT: first output a fenced ```json block with "
    "keys parties (array), effective_date, end_date, term, contract_value, "
    "governing_law, key_obligations (array); then a short prose summary.\n"
    "For any other image, just answer the user's question in prose.\n"
    "Use null for fields that are not visible. Never invent values."
)


def to_data_url(image_base64: str) -> str:
    """Return a data: URL for the image; frontend may send raw base64 or a
    full data URL — both are accepted."""
    raw = image_base64.strip()
    if _DATA_URL_RE.match(raw):
        return raw
    return f"data:image/png;base64,{raw}"


def build_user_content(
    message: str, images: "str | Sequence[str]"
) -> List[dict]:
    """OpenAI multimodal content parts: text + one image_url per image
    (2026-08-05: the composer sends up to 5). A bare string is accepted for
    the single-image callers that predate the list form."""
    imgs = [images] if isinstance(images, str) else list(images)
    return [
        {
            "type": "text",
            "text": message
            or (
                "Describe this image."
                if len(imgs) <= 1
                else "Describe these images."
            ),
        },
        *(
            {"type": "image_url", "image_url": {"url": to_data_url(i)}}
            for i in imgs
        ),
    ]


def extract_json_block(text: str) -> Optional[dict]:
    m = _JSON_BLOCK_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


async def run_vision_engine(
    message: str,
    images: "Optional[str | Sequence[str]]",
    history: Sequence[dict],
    emit: Emit,
) -> str:
    imgs = [images] if isinstance(images, str) else list(images or [])
    if not imgs:
        raise ValueError("the vision engine requires an attached image")

    user_content = build_user_content(message, imgs)

    # Unlimited-OCR pass (2026-08-06): screenshots, invoices and photographed
    # documents get a dedicated OCR transcript alongside the pixels — the OCR
    # model reads dense text/tables the general VLM misses. Photos with no
    # text produce an empty transcript and add nothing.
    from ..config import settings
    from .ocr import ocr_images, transcript_block

    if settings.ocr_enabled:
        transcripts = await ocr_images(imgs)
        block = transcript_block(transcripts, "image")
        if block:
            user_content.append(
                {
                    "type": "text",
                    "text": block
                    + "\n(Transcript from the OCR model — if it disagrees "
                    "with the pixels, trust the pixels.)",
                }
            )

    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_content},
    ]

    parts: List[str] = []
    # Reasoning-aware stream (the vision model is the thinking main model):
    # surface thinking in the panel and give room to finish the answer.
    async for kind, delta in llm.stream_chat_events(
        messages, model_choice="smart", effort="medium", max_tokens=8000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    answer = "".join(parts)

    # §10: the single final meta carries only contract keys. The structured
    # invoice/contract fields already reach the user inside the streamed
    # answer's fenced ```json block; there is no `extracted` meta key in the
    # contract, so nothing else is emitted.
    await emit("meta", {"route": "vision"})
    return answer
