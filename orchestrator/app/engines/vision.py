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
from ..config import settings

Emit = Callable[[str, dict], Awaitable[None]]

#: Effort this engine runs at when a caller does not name one. "think" is
#: exactly what this route did before 2026-08-28, so every caller that is not
#: updated (graph.py's `_vision_node`, bare API calls) keeps today's
#: behaviour and nothing regresses.
DEFAULT_EFFORT = "think"

#: Completion ceiling asked of the vision model. Deliberately generous; see
#: `vision_max_tokens` for why frugal is the wrong instinct on this route.
VISION_ANSWER_TOKENS = 8000

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


def vision_max_tokens(max_tokens: Optional[int] = None) -> int:
    """Completion ceiling for one vision call, clamped to what the vision
    endpoint says it will serve (`VISION_OUTPUT_LIMIT`, 8192 here).

    WHY THE BUDGET MATTERS MORE HERE THAN ANYWHERE ELSE: reasoning and answer
    are drawn from ONE `max_tokens` pool, and an image prompt makes the model
    think long before it writes anything. Measured 2026-08-28 on three
    1280x800 screenshots: with thinking ON and max_tokens=700 the stream
    produced ZERO content chunks — 26-28s of reasoning, whole budget gone, no
    answer at all. The same three images with thinking OFF answered in
    2.3-2.9s. So a small ceiling here does not make the route cheaper, it
    makes it return nothing.

    `llm.stream_chat_events` already protects the thinking-ON case (it floors
    the request at MAX_OUTPUT_TOKENS whenever thinking is on), which makes
    this value the ceiling the FAST path actually runs under. It has to be
    big enough for a full multi-image extraction to finish, hence generous.

    Clamping keeps the request coherent with the declared capability: a
    deployment that lowers VISION_OUTPUT_LIMIT is never asked for more than
    it serves, and a caller that wants less (or the request-level budget a
    future caller passes in) is honoured as-is.
    """
    want = max_tokens if max_tokens and max_tokens > 0 else VISION_ANSWER_TOKENS
    limit = settings.vision_capabilities.output_limit or 0
    return min(want, limit) if limit > 0 else want


async def run_vision_engine(
    message: str,
    images: "Optional[str | Sequence[str]]",
    history: Sequence[dict],
    emit: Emit,
    *,
    effort: str = DEFAULT_EFFORT,
    max_tokens: Optional[int] = None,
) -> str:
    """Answer about attached image(s) at the effort the caller asked for.

    `effort` accepts any wire value `llm.normalize_effort` understands and
    means the same thing it means on the text route: fast -> thinking OFF,
    think/max -> thinking ON. It is NOT a knob invented here — the whole
    mechanism is `stream_chat_events` turning the effort into the chat
    template's `enable_thinking`; this engine's only job is to stop
    overriding it.
    """
    imgs = [images] if isinstance(images, str) else list(images or [])
    if not imgs:
        raise ValueError("the vision engine requires an attached image")

    user_content = build_user_content(message, imgs)

    # Unlimited-OCR pass (2026-08-06): screenshots, invoices and photographed
    # documents get a dedicated OCR transcript alongside the pixels — the OCR
    # model reads dense text/tables the general VLM misses. Photos with no
    # text produce an empty transcript and add nothing.
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
    # Reasoning-aware stream (the vision model IS the thinking main model):
    # surface thinking in the panel and give room to finish the answer.
    #
    # Until 2026-08-28 this call hard-coded effort="medium" — an alias for
    # "think" — so every image upload ran the 27B with thinking ON no matter
    # which of Fast/Think/Max the user picked in the composer. Measured on
    # three 1280x800 screenshots: thinking on -> 26-28s to the first visible
    # token and no answer inside the budget; thinking off -> 2.3-2.9s and a
    # complete answer. Passing the request's effort through is the entire
    # fix; `stream_chat_events` already maps it to `enable_thinking`.
    #
    # model_choice stays "smart" on purpose: the 27B is the vision model on
    # this deployment (VISION_BASE_URL == OPENAI_BASE_URL). The dedicated 8B
    # VL model is NOT a fallback — measured 2026-08-28 it was slower to first
    # token AND refused the extraction outright.
    level = llm.normalize_effort(effort)
    async for kind, delta in llm.stream_chat_events(
        messages,
        model_choice="smart",
        effort=level,
        max_tokens=vision_max_tokens(max_tokens),
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
