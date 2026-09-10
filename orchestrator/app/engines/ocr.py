"""Unlimited-OCR client (2026-08-06).

baidu/Unlimited-OCR (3.3B document-OCR VLM, MIT) on its own vLLM service
(`vllm-ocr`, native UnlimitedOCRForCausalLM support in vLLM ≥0.26). It reads
scans, invoices, tables and handwriting far better than a general VLM, so
uploaded images and rendered PDF pages are transcribed HERE first and the
transcript is handed to the main model alongside the pixels.

Failure policy: OCR is an enhancer, never a gate. Any error — service down,
timeout, model still loading — yields an empty transcript for that image and
the caller proceeds pixels-only, exactly like before OCR existed.

BUT '' IS NOT A FACT ABOUT THE IMAGE. For a chat turn, "no transcript" and
"nothing written in the picture" lead to the same answer, so `ocr_images`
flattening both to '' costs nothing. For video they are opposite claims: one
says the screen was blank, the other says nobody read it, and the second must
never be cached as the first (2026-09-10). `read_images` therefore answers
with an `OcrRead` per image — text plus one of ok / empty / degenerate /
failed — and `ocr_images` stays the flattened view for the callers that
genuinely do not care.

DEGENERATE OUTPUT is the third answer this model gives. Measured against the
live engine on 2026-09-11 with real video frames: the app's prompt ("document
parsing") looped on four of six frames — 3,747 characters of "nije nije nije"
for a slide whose text is two lines — and every one of its answers began with
a hallucinated "ovi…" prefix, while the prompt "OCR" read the same frames
correctly in a tenth of the time. A loop is not a transcript and not an empty
screen: it is a failed read, and `is_degenerate` is how a caller can tell.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import zlib
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..config import settings

log = logging.getLogger(__name__)

# The model card's document prompt. "document parsing" returns markdown-ish
# structured text (tables included); no detection boxes are requested.
_DEFAULT_PROMPT = "document parsing"


def document_prompt() -> str:
    """The prompt for a DOCUMENT page or an uploaded image.

    THE DEFAULT IS DELIBERATELY UNCHANGED, and it is worth knowing why.
    Measured on 2026-09-11 against the engine now on the worker Spark, with
    one real PDF page from this deployment: "document parsing" came back with
    16,775 characters at a unique-token ratio of 0.002 — a loop, and 42
    seconds of GPU — where "OCR" returned 948 characters of ordinary varied
    text in 2.5 seconds. That is the same fault the video frames showed.

    But one page is not a corpus, the document route belongs to another
    surface, and flipping a prompt under every scanned invoice on the
    strength of one sample is not a fix. So the lever is here and the
    default is not touched: OCR_PROMPT overrides it for a deployment whose
    operator has decided, and the video path (`screen.video_ocr_prompt`)
    already sends the prompt that was measured to work.
    """
    return (os.environ.get("OCR_PROMPT") or "").strip() or _DEFAULT_PROMPT

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


# ------------------------------------------------------------ degeneracy --

#: A read is degenerate when the model stopped reading and started repeating.
#: Every threshold here was set against this engine's own output on 2026-09-11
#: — 740 stored video transcripts plus ten fresh reads — because the cost of
#: the two mistakes is not symmetric: calling a loop a transcript poisons the
#: evidence, and calling a transcript a loop throws away text that was really
#: on the screen.
#:
#: What that measurement showed:
#:   * the loops repeat ONE token or line for almost the whole answer
#:     ("nije" 700 times in 749 tokens) and score a unique-token ratio of
#:     0.001-0.017;
#:   * this model legitimately repeats its own placeholders — a browser
#:     window comes back as real text plus "[Non-Text]" eight times — so a
#:     run of those says nothing about the read and is excluded;
#:   * a run that is real but short (a repeated LaTeX line inside an
#:     otherwise good read) must not condemn the whole transcript, so a run
#:     also has to DOMINATE the answer.
_DEGENERATE_MIN_TOKENS = 60
_DEGENERATE_UNIQUE_RATIO = 0.08
_DEGENERATE_TOKEN_RUN = 12
_DEGENERATE_LINE_RUN = 8
_DEGENERATE_RUN_SHARE = 0.4
_DEGENERATE_CHAR_RUN = 60
#: The loop is not always a whole word: "ovišatiševanjaševanjaševanja…" runs
#: 2,600 characters with no space in it, which no rule above can see. Every
#: repetition of any unit is compressible, so the last rule is the deflate
#: ratio: the stored loops came in at 0.02-0.04 while real text — this
#: engine's correct reads, and the speech transcripts beside them — stayed
#: above 0.27.
_DEGENERATE_MIN_CHARS = 400
_DEGENERATE_COMPRESSION = 0.08
#: "[Non-Text]", "[NO TEXT]", "[Image]" — the model's own placeholders for a
#: region it read as having no text. Content, for the purpose of judging
#: repetition, is what is left after they are taken out.
_PLACEHOLDER_RE = re.compile(r"^\[[^\]]{0,24}\]$")


def _longest_run(items: Sequence[str]) -> int:
    """The longest run of one value repeated back to back."""
    best = run = 0
    previous: Optional[str] = None
    for item in items:
        run = run + 1 if item == previous else 1
        previous = item
        best = max(best, run)
    return best


def _dominates(run: int, total: int, floor: int) -> bool:
    """A repetition counts only when it is long AND most of the answer."""
    return run >= floor and total > 0 and run >= _DEGENERATE_RUN_SHARE * total


def is_degenerate(text: str) -> bool:
    """True when this transcript is the model looping, not the image's text."""
    body = (text or "").strip()
    if not body:
        return False
    if re.search(r"(.)\1{%d,}" % _DEGENERATE_CHAR_RUN, body):
        return True
    tokens = [t for t in body.split() if not _PLACEHOLDER_RE.match(t)]
    if _dominates(_longest_run(tokens), len(tokens), _DEGENERATE_TOKEN_RUN):
        return True
    lines = [
        ln.strip() for ln in body.splitlines()
        if len(ln.strip()) > 1 and not _PLACEHOLDER_RE.match(ln.strip())
    ]
    if _dominates(_longest_run(lines), len(lines), _DEGENERATE_LINE_RUN):
        return True
    if len(tokens) >= _DEGENERATE_MIN_TOKENS and len(set(tokens)) / len(tokens) < _DEGENERATE_UNIQUE_RATIO:
        return True
    if len(body) >= _DEGENERATE_MIN_CHARS:
        raw = body.encode("utf-8", "replace")
        if len(zlib.compress(raw, 6)) / len(raw) < _DEGENERATE_COMPRESSION:
            return True
    return False


@dataclass(frozen=True)
class OcrRead:
    """One image's transcript and what happened to it.

    `status` is one of:
      ok          text was read;
      empty       the model answered and there was nothing legible;
      degenerate  the model looped — a FAILED read, never an empty screen;
      failed      the call raised, or the batch deadline passed first.
    `text` is kept for a degenerate read so a human can see what came back,
    but no caller may treat it as evidence.
    """

    text: str
    status: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def read(self) -> bool:
        """Did the image get read at all (as opposed to failing to be read)?"""
        return self.status in ("ok", "empty")

    def to_json(self) -> dict:
        return {"text": self.text, "status": self.status, "error": self.error}


def classify(text: str) -> OcrRead:
    """A raw (cleaned) transcript -> the read it actually is."""
    body = (text or "").strip()
    if not body:
        return OcrRead("", "empty")
    if is_degenerate(body):
        return OcrRead(body, "degenerate", "the OCR model repeated itself instead of reading the image")
    return OcrRead(body, "ok")


def _to_data_url(image_base64: str) -> str:
    raw = image_base64.strip()
    if raw.startswith("data:"):
        return raw
    return f"data:image/png;base64,{raw}"


async def _ocr_one(
    client, image_base64: str, max_tokens: Optional[int] = None, *, prompt: Optional[str] = None
) -> str:
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
                    # The prompt is a per-caller argument because it changes
                    # what this model DOES: see the module docstring, and
                    # `screen.video_ocr_prompt` for the one video sends.
                    {"type": "text", "text": prompt or document_prompt()},
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


async def read_images(
    images: Sequence[str],
    *,
    prompt: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    deadline_s: Optional[float] = None,
) -> List[OcrRead]:
    """Transcribe each image and say, per image, what happened.

    Order is preserved and there is exactly one `OcrRead` per input. Nothing
    here raises for an image: an image that could not be read comes back
    `failed` with the reason, so a caller can tell "the screen was blank"
    from "nobody read the screen".

    `max_output_tokens` and `deadline_s` (2026-09-03) exist for the
    INTERACTIVE image route: the transcript is drafted before the main model
    can start, and on a text-dense screenshot the sidecar measured 47 s of
    decoding ahead of the first visible token. Past the deadline the images
    still in flight are cancelled and marked failed — the ones that finished
    keep their text, which is new: the whole batch used to be dropped.
    Document pages (the PDF route) pass neither and keep the full budget: a
    scanned page has no other way to be read.
    """
    if not images:
        return []
    if not settings.ocr_enabled:
        return [OcrRead("", "failed", "OCR is not enabled on this deployment") for _ in images]

    from .. import llm

    client = llm._client(settings.ocr_base_url)
    sem = asyncio.Semaphore(concurrency())

    async def guarded(idx: int, img: str) -> OcrRead:
        async with sem:
            try:
                # The prompt is passed ONLY when a caller overrode it, so the
                # default call is the three-argument one this module has always
                # made — a stub or a wrapper written against the old signature
                # keeps working.
                extra = {"prompt": prompt} if prompt else {}
                raw = await _ocr_one(client, img, max_output_tokens, **extra)
            except Exception as exc:  # noqa: BLE001 — enhancer, never a gate
                log.warning("OCR failed for image %d: %s", idx, exc)
                return OcrRead("", "failed", f"{type(exc).__name__}: {str(exc)[:200]}")
        read = classify(raw)
        if read.status == "degenerate":
            log.warning(
                "OCR returned %d characters of repetition for image %d (prompt %r): %s",
                len(read.text), idx, prompt or document_prompt(), read.text[:80],
            )
        return read

    tasks = [asyncio.ensure_future(guarded(i, img)) for i, img in enumerate(images)]
    timed_out = False
    try:
        if deadline_s and deadline_s > 0:
            _done, pending = await asyncio.wait(tasks, timeout=float(deadline_s))
            if pending:
                timed_out = True
                log.info(
                    "OCR did not finish %d of %d image(s) within %.0fs",
                    len(pending), len(images), deadline_s,
                )
                await _cancel(pending)
        else:
            await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        # The caller went away (a stage timeout, a shutdown). No transcript
        # of this batch will be read, so no image may keep decoding.
        await _cancel(tasks)
        raise
    late = (
        f"the OCR batch deadline of {float(deadline_s):.0f}s passed before this image was read"
        if timed_out
        else "the OCR batch ended before this image was read"
    )
    out: List[OcrRead] = []
    for task in tasks:
        if task.done() and not task.cancelled() and task.exception() is None:
            out.append(task.result())
        else:
            out.append(OcrRead("", "failed", late))
    return out


async def _cancel(tasks) -> None:
    """Stop the images still in flight and wait for them to stop."""
    pending = [t for t in tasks if not t.done()]
    for task in pending:
        task.cancel()
    if not pending:
        return
    try:
        await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
    except asyncio.CancelledError:
        pass


async def ocr_images(
    images: Sequence[str],
    *,
    max_output_tokens: Optional[int] = None,
    deadline_s: Optional[float] = None,
) -> List[str]:
    """Transcribe each image (base64 or data: URL) with Unlimited-OCR.

    Returns one transcript per input, order preserved; '' where OCR was
    disabled or failed — the caller must treat '' as "no transcript", never
    as an error. Callers that need to tell those apart (video) use
    `read_images` instead.

    A degenerate read keeps its text here so that this function's answer is
    the same one the document and interactive image routes have always got;
    the loop is logged by `read_images` and reported as a failed read to
    anyone who asks for the structured form.
    """
    reads = await read_images(
        images, max_output_tokens=max_output_tokens, deadline_s=deadline_s
    )
    return [r.text if r.status in ("ok", "degenerate") else "" for r in reads]


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
