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
from typing import List, Sequence

from ..config import settings

log = logging.getLogger(__name__)

# The model card's document prompt. "document parsing" returns markdown-ish
# structured text (tables included); no detection boxes are requested.
_PROMPT = "document parsing"

# Defaults when the capability block is missing: at most this many
# pages/images transcribed concurrently (the OCR service has a small memory
# slice and one uploaded PDF can be 8 pages), and the output ceiling that
# fits the server's 8192 window next to ~1.5-2K image tokens. Since
# 2026-08-29 both follow OCR_CONCURRENCY / OCR_OUTPUT_LIMIT when set — the
# values were hard-coded before and silently ignored the configuration.
_CONCURRENCY = 3
_MAX_OUTPUT_TOKENS = 6000

_TIMEOUT_S = 120.0


def _capability(name: str, default: int) -> int:
    caps = getattr(settings, "ocr_capabilities", None) or getattr(
        getattr(settings, "model_capabilities", None), "ocr", None
    )
    value = int(getattr(caps, name, 0) or 0)
    return value if value > 0 else default


def output_limit() -> int:
    """Tokens the OCR model may emit per image (never above the window fit)."""
    return min(_MAX_OUTPUT_TOKENS, _capability("output_limit", _MAX_OUTPUT_TOKENS))


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


async def _ocr_one(client, image_base64: str) -> str:
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
        # The OCR server runs a tight 8192 window (memory budget); image
        # tokens + prompt take ~1.5-2K, so 6000 is the hard ceiling and
        # OCR_OUTPUT_LIMIT (2048 in production) the configured one.
        max_tokens=output_limit(),
        temperature=0.0,
        timeout=_TIMEOUT_S,
    )
    return clean_transcript(resp.choices[0].message.content or "")


async def ocr_images(images: Sequence[str]) -> List[str]:
    """Transcribe each image (base64 or data: URL) with Unlimited-OCR.

    Returns one transcript per input, order preserved; '' where OCR was
    disabled or failed — the caller must treat '' as "no transcript", never
    as an error.
    """
    if not settings.ocr_enabled or not images:
        return ["" for _ in images]

    from .. import llm

    client = llm._client(settings.ocr_base_url)
    sem = asyncio.Semaphore(concurrency())

    async def guarded(idx: int, img: str) -> str:
        async with sem:
            try:
                return await _ocr_one(client, img)
            except Exception as exc:  # noqa: BLE001 — enhancer, never a gate
                log.warning("OCR failed for image %d: %s", idx, exc)
                return ""

    return list(
        await asyncio.gather(*(guarded(i, img) for i, img in enumerate(images)))
    )


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
