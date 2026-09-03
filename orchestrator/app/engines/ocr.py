"""Unlimited-OCR client (2026-08-06).

baidu/Unlimited-OCR (3.3B document-OCR VLM, MIT) on its own vLLM service
(`vllm-ocr`, native UnlimitedOCRForCausalLM support in vLLM ≥0.26). It reads
scans, invoices, tables and handwriting far better than a general VLM, so
uploaded images and rendered PDF pages are transcribed HERE first and the
transcript is handed to the main model alongside the pixels.

Failure policy: OCR is an enhancer, never a gate. Any error — service down,
timeout, model still loading — yields an empty transcript for that image and
the caller proceeds pixels-only, exactly like before OCR existed.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional, Sequence

from ..config import settings

log = logging.getLogger(__name__)

# The model card's document prompt. "document parsing" returns markdown-ish
# structured text (tables included); no detection boxes are requested.
_PROMPT = "document parsing"

# At most this many pages/images transcribed concurrently — the OCR service
# has a small memory slice and one uploaded PDF can be 8 pages. Follows
# OCR_CONCURRENCY when the profile declares one (2026-08-29; it was hard-coded
# and silently ignored the configuration).
_CONCURRENCY = 3

# The output ceiling is derived from the OCR model's WINDOW, not from
# OCR_OUTPUT_LIMIT. That variable looks like an OCR tuning but is not one: the
# launcher emits `<ROLE>_OUTPUT_LIMIT = min(8192, max(256, context // 4))` for
# every role generically, so on this deployment it is 8192 // 4 = 2048 —
# a third of what the window actually affords, which would truncate dense
# scans and tables mid-table with no signal. Measured prompt sizes on the
# served model are 487-1807 tokens per page, so reserving 2200 leaves ~6000
# in an 8192 window; that reserve is what the old hard-coded 6000 encoded.
_CONTEXT_TOKENS = 8192
_PROMPT_RESERVE_TOKENS = 2200
_MAX_OUTPUT_TOKENS = 6000
_MIN_OUTPUT_TOKENS = 512

_TIMEOUT_S = 120.0


def _capability(name: str, default: int) -> int:
    caps = getattr(settings, "ocr_capabilities", None) or getattr(
        getattr(settings, "model_capabilities", None), "ocr", None
    )
    value = int(getattr(caps, name, 0) or 0)
    return value if value > 0 else default


def output_limit() -> int:
    """Tokens the OCR model may emit per image, from its declared window.

    Shrinks with a smaller OCR_CONTEXT_LENGTH (the reserve still fits) and
    never exceeds the measured 6000.
    """
    window = _capability("context_length", _CONTEXT_TOKENS)
    room = window - _PROMPT_RESERVE_TOKENS
    return max(_MIN_OUTPUT_TOKENS, min(_MAX_OUTPUT_TOKENS, room))


def concurrency() -> int:
    return max(1, _capability("concurrency", _CONCURRENCY))

# Detection blocks are pure layout metadata — "<|det|>type [bbox]<|/det|>"
# per the model card — so the WHOLE block goes, not just its markers.
_DET_RE = re.compile(r"<\|det\|>.*?<\|/det\|>", re.S)
# Any other stray control tokens: strip the markers, keep wrapped text.
_TAG_RE = re.compile(r"<\|/?[a-z_]+\|>")
# Bare bbox payloads like [[123, 45, 678, 90]] left outside det blocks.
_BBOX_RE = re.compile(r"\[\[\d+(?:,\s*\d+){3}\]\]")
# The format the served model ACTUALLY emits (observed live 2026-08-06):
# each line starts "type [x, y, x, y]Content" — e.g.
# "text [31, 306, 212, 347]Vendor: TechSara". Strip the region-type word and
# bbox, keep Content.
_LINE_REGION_RE = re.compile(
    r"^\s*(?:[a-z_]{1,12}\s*)?\[\d+(?:,\s*\d+){3}\]\s*", re.M
)


def clean_transcript(raw: str) -> str:
    """Drop layout-control markup, keep the recognized text."""
    out = _DET_RE.sub("", raw or "")
    out = _TAG_RE.sub("", out)
    out = _BBOX_RE.sub("", out)
    out = _LINE_REGION_RE.sub("", out)
    return out.strip()


def _to_data_url(image_base64: str) -> str:
    raw = image_base64.strip()
    if raw.startswith("data:"):
        return raw
    return f"data:image/png;base64,{raw}"


async def _ocr_one(client, image_base64: str, max_tokens: Optional[int] = None) -> str:
    resp = await client.chat.completions.create(
        model=settings.ocr_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": _to_data_url(image_base64)},
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
        # Derived from the OCR window; see output_limit(). A caller on the
        # interactive image route asks for less — see ocr_images().
        max_tokens=min(output_limit(), max_tokens) if max_tokens else output_limit(),
        temperature=0.0,
        timeout=_TIMEOUT_S,
    )
    choice = resp.choices[0]
    text = clean_transcript(choice.message.content or "")
    if getattr(choice, "finish_reason", None) == "length" and text:
        # A page denser than the output ceiling comes back cut off mid-content.
        # Saying so is the difference between the main model treating the tail
        # as absent and treating it as "not transcribed here" (2026-08-29).
        text += "\n[transcript truncated at the OCR output limit]"
    return text


async def ocr_images(
    images: Sequence[str],
    *,
    max_output_tokens: Optional[int] = None,
    deadline_s: Optional[float] = None,
) -> List[str]:
    """Transcribe each image (base64 or data: URL) with Unlimited-OCR.

    Returns one transcript per input, order preserved; '' where OCR was
    disabled or failed — the caller must treat '' as "no transcript", never
    as an error.

    `max_output_tokens` and `deadline_s` (2026-09-03) exist for the
    INTERACTIVE image route: the transcript is drafted before the main model
    can start, and on a text-dense screenshot the sidecar measured 47 s of
    decoding ahead of the first visible token. Past the deadline every
    transcript still in flight is dropped and the caller proceeds
    pixels-only — the main model reads the screenshot itself. Document
    pages (the PDF route) pass neither and keep the full budget: a scanned
    page has no other way to be read.
    """
    if not settings.ocr_enabled or not images:
        return ["" for _ in images]

    from .. import llm

    client = llm._client(settings.ocr_base_url)
    sem = asyncio.Semaphore(concurrency())

    async def guarded(idx: int, img: str) -> str:
        async with sem:
            try:
                return await _ocr_one(client, img, max_output_tokens)
            except Exception as exc:  # noqa: BLE001 — enhancer, never a gate
                log.warning("OCR failed for image %d: %s", idx, exc)
                return ""

    work = asyncio.gather(*(guarded(i, img) for i, img in enumerate(images)))
    if deadline_s and deadline_s > 0:
        try:
            return list(await asyncio.wait_for(work, timeout=float(deadline_s)))
        except asyncio.TimeoutError:
            log.info(
                "OCR did not finish %d image(s) within %.0fs — answering from the pixels",
                len(images), deadline_s,
            )
            return ["" for _ in images]
    return list(await work)


def transcript_block(transcripts: Sequence[str], label: str) -> str:
    """Format non-empty transcripts as one context block for the main model.

    Returns '' when every transcript is empty so callers can skip the section
    entirely (the model must not see an empty "OCR transcript:" header).
    """
    if not any(t.strip() for t in transcripts):
        return ""
    if len(transcripts) == 1:
        body = transcripts[0].strip() or "(nothing legible)"
        return f"\n\nOCR transcript of the {label} (Unlimited-OCR):\n{body}"
    parts = [f"\n\nOCR transcript of the {label} (Unlimited-OCR):"]
    for i, t in enumerate(transcripts, 1):
        parts.append(f"\n--- {label.capitalize()} {i} ---\n{t.strip() or '(nothing legible)'}")
    return "\n".join(parts)
