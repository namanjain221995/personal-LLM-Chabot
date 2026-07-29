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


def build_user_content(message: str, image_base64: str) -> List[dict]:
    """OpenAI multimodal content parts: text + image_url (data: URL)."""
    return [
        {"type": "text", "text": message or "Describe this image."},
        {"type": "image_url", "image_url": {"url": to_data_url(image_base64)}},
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
    image_base64: Optional[str],
    history: Sequence[dict],
    emit: Emit,
) -> str:
    if not image_base64:
        raise ValueError("the vision engine requires an attached image")

    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": build_user_content(message, image_base64)},
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
