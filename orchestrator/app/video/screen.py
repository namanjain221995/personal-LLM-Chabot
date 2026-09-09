"""What was on screen: OCR text and a caption for every kept frame.

TWO READERS, BOTH OPTIONAL, NEITHER A GATE.

* OCR — baidu/Unlimited-OCR through `engines.ocr.ocr_images`, the same
  client the PDF route uses for scanned pages. It reads dense text, tables
  and code far better than a general vision model; its output is the
  verbatim on-screen text and it is what "read the code on the slide"
  quotes from. Frames go in batches of the OCR concurrency with a per-batch
  deadline, because a text-dense 1280x720 frame has cost 47 s of decoding
  on this deployment and a hung batch must not hang the video.

* Captions — the router vision model (Qwen3-VL-8B), asked for a frame TYPE
  and an ~80-word description. Measured 2026-09-09: ~2 s a frame at 896 px,
  0.6 s effective at four in flight, and it reads code verbatim. It is the
  router rather than the main model so that captioning a hundred frames
  never competes with the chat model that everyone is waiting on. The main
  model, which can also see, is kept for QUESTION time — one or two frames
  when a question is visual — where its quality matters most and its cost is
  bounded.

Both readers return '' / None on failure and the video is understood from
whatever survived. A screen recording with a muted microphone is understood
from these alone; a talking head with nothing on screen gets a caption per
distinct shot and nothing to OCR.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Awaitable, Callable, List, Optional, Sequence

from ..config import settings
from .frames import KeptFrame
from .types import OcrSpan

log = logging.getLogger(__name__)


def _router_enabled() -> bool:
    """The router sidecar is optional on some profiles; its capability record says."""
    caps = getattr(settings, "router_capabilities", None)
    return bool(getattr(caps, "enabled", True)) and bool(settings.router_base_url)

Progress = Callable[[Optional[float], str], Awaitable[None]]

FRAME_KINDS = ("slide", "code", "terminal", "webpage", "document", "person", "diagram", "other")

_CAPTION_PROMPT = (
    "You are describing ONE frame of a video for a searchable index. Answer in exactly "
    "three lines and nothing else:\n"
    "TYPE: one of slide, code, terminal, webpage, document, person, diagram, other\n"
    "SHOWS: what is on screen, in at most 60 words, factual, no speculation\n"
    "TEXT: the most important readable text, verbatim, or NONE"
)

_TYPE_RE = re.compile(r"^\s*TYPE\s*:\s*([a-z_]+)", re.I | re.M)
_SHOWS_RE = re.compile(r"^\s*SHOWS\s*:\s*(.+)$", re.I | re.M)
_TEXT_RE = re.compile(r"^\s*TEXT\s*:\s*(.+)$", re.I | re.M | re.S)


def _data_url(path: str, *, max_width: Optional[int] = None) -> str:
    """The frame as a JPEG data URL, optionally downscaled for the reader."""
    if max_width:
        from PIL import Image
        import io

        with Image.open(path) as im:
            if im.width > max_width:
                im = im.resize((max_width, int(im.height * max_width / im.width)))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=85)
            raw = buf.getvalue()
    else:
        with open(path, "rb") as fh:
            raw = fh.read()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


# --------------------------------------------------------------------- OCR --

_TRUNCATED_MARK = "[transcript truncated at the OCR output limit]"


def _clean_ocr(text: str) -> str:
    """Drop the client's truncation marker and collapse whitespace runs; the
    marker is a note to a prompt, not on-screen text, and must not be indexed
    as evidence."""
    text = (text or "").replace(_TRUNCATED_MARK, "").strip()
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: List[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()


async def ocr_frames(
    frames: Sequence[KeptFrame], *, progress: Progress
) -> List[str]:
    """One OCR transcript per frame ('' when nothing legible / failed)."""
    from ..engines import ocr

    if not settings.ocr_enabled or not settings.video_ocr_enabled or not frames:
        return ["" for _ in frames]
    batch = max(1, settings.video_ocr_concurrency)
    out: List[str] = []
    from . import pipeline as _pipe

    for i in range(0, len(frames), batch):
        await _pipe.pace()
        chunk = frames[i : i + batch]
        urls = await asyncio.to_thread(lambda c=chunk: [_data_url(f.path) for f in c])
        texts = await ocr.ocr_images(
            urls,
            max_output_tokens=settings.video_ocr_max_tokens,
            deadline_s=settings.video_ocr_batch_deadline_s,
        )
        out.extend(_clean_ocr(t) for t in texts)
        await progress(100.0 * len(out) / len(frames), f"{len(out)}/{len(frames)} frames")
    return out


# ---------------------------------------------------------------- captions --


async def _caption_one(client, url: str) -> Optional[str]:
    from .. import llm

    request = dict(
        model=settings.router_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": _CAPTION_PROMPT},
                ],
            }
        ],
        max_tokens=settings.video_caption_max_tokens,
        temperature=0.0,
        timeout=settings.video_caption_timeout_s,
    )
    # The router's chat template thinks unless told not to; a caption must
    # not spend its budget reasoning.
    extra = llm.reasoning_extra_body(settings.router_capabilities, False)
    if extra is not None:
        request["extra_body"] = extra
    resp = await client.chat.completions.create(**request)
    return (resp.choices[0].message.content or "").strip() or None


async def caption_frames(
    frames: Sequence[KeptFrame], *, progress: Progress
) -> List[Optional[str]]:
    """One caption per frame, or None where the reader failed."""
    if not settings.video_captions_enabled or not _router_enabled() or not frames:
        return [None for _ in frames]
    from .. import llm

    client = llm._client(settings.router_base_url)
    sem = asyncio.Semaphore(max(1, settings.video_caption_concurrency))
    results: List[Optional[str]] = [None] * len(frames)
    done = 0

    from . import pipeline as _pipe

    async def one(i: int, frame: KeptFrame) -> None:
        nonlocal done
        async with sem:
            await _pipe.pace()
            try:
                url = await asyncio.to_thread(_data_url, frame.path, max_width=settings.video_caption_width)
                results[i] = await _caption_one(client, url)
            except Exception as exc:  # noqa: BLE001 — a reader, never a gate
                log.warning("caption failed for frame %d: %s", i, exc)
                results[i] = None
            done += 1
            if done % 4 == 0 or done == len(frames):
                await progress(100.0 * done / len(frames), f"{done}/{len(frames)} frames")

    await asyncio.gather(*(one(i, f) for i, f in enumerate(frames)))
    return results


def parse_caption(raw: Optional[str]) -> tuple:
    """-> (kind, description, text). Tolerant of a model that skips a line."""
    if not raw:
        return "", "", ""
    kind = ""
    m = _TYPE_RE.search(raw)
    if m:
        candidate = m.group(1).lower().replace("-", "_")
        kind = candidate if candidate in FRAME_KINDS else "other"
    shows = ""
    m = _SHOWS_RE.search(raw)
    if m:
        shows = m.group(1).strip()
    text = ""
    m = _TEXT_RE.search(raw)
    if m:
        text = m.group(1).strip()
        if text.upper().startswith("NONE"):
            text = ""
    if not shows and not kind:
        shows = raw.strip()[:400]
    return kind, shows, text


# ------------------------------------------------------------------ spans --


def _same_text(a: str, b: str) -> bool:
    """Near-identical OCR outputs — a slide with the cursor in a different
    place reads the same but hashed differently."""
    na, nb = " ".join(a.split()), " ".join(b.split())
    if not na or not nb:
        return na == nb
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) < 40:
        return False
    # A cheap containment ratio, no external library.
    return shorter in longer and len(shorter) / len(longer) > 0.9


def build_spans(
    frames: Sequence[KeptFrame],
    ocr_texts: Sequence[str],
    captions: Sequence[Optional[str]],
) -> List[OcrSpan]:
    """Frames + what the readers said -> spans, merging repeats.

    Consecutive frames whose on-screen TEXT is the same merge into one span
    (start of the first, end of the last) so "the pricing table was up from
    1:22 to 7:40" survives as one fact. A frame with no text and no caption
    still produces a span, so the timeline has no holes — the fusion step
    sees "1:00-1:30: nothing legible on screen" rather than nothing.
    """
    spans: List[OcrSpan] = []
    for frame, text, raw_caption in zip(frames, ocr_texts, captions):
        kind, shows, cap_text = parse_caption(raw_caption)
        text = text or ""
        if not text.strip() and cap_text:
            # The OCR model saw nothing legible but the captioner read
            # something: keep it as text, marked so a reader knows the source.
            text = cap_text
        caption = shows or None
        if spans and _same_text(spans[-1].text, text) and text.strip():
            prev = spans[-1]
            spans[-1] = OcrSpan(
                start_s=prev.start_s,
                end_s=frame.end_s,
                text=prev.text if len(prev.text) >= len(text) else text,
                kind=prev.kind or kind,
                caption=prev.caption or caption,
                frame=prev.frame,
                phash=prev.phash,
            )
            continue
        spans.append(
            OcrSpan(
                start_s=frame.t_s,
                end_s=frame.end_s,
                text=text,
                kind=kind,
                caption=caption,
                frame=frame.path.rsplit("/", 1)[-1],
                phash=frame.phash,
            )
        )
    return spans
