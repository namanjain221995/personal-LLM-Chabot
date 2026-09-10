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

WHAT WAS NOT READ IS NOT A BLANK SCREEN. Until 2026-09-10 an OCR outage, a
batch deadline and a slide with no words produced the same '' per frame, the
stage was stamped done, and — because analyses are content-addressed — that
answer was served from cache for those bytes forever. `read_frames` returns
the per-frame status alongside the text and a stage-level summary, so the
pipeline can record a failed read as a failed read and fusion can say which
reader was missing.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

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

#: What the OCR engine is asked for a video FRAME.
#:
#: MEASURED, NOT ASSUMED (2026-09-11, against the engine now running on the
#: worker Spark). The prompt the client has always sent, "document parsing"
#: — the model card's own — makes this model loop on screenshots: on six real
#: frames from finished analyses it looped on four, returning 3,747
#: characters of "nije nije nije" for a slide whose text is two lines, and
#: every one of its answers, looping or not, opened with a hallucinated
#: "ovi…" prefix. The prompt "OCR" read the same frames correctly and in a
#: tenth of the time ("Weekly Planning Meeting / Agenda / 1. Pricing for the
#: Meryton launch"). Of the 740 stored frame transcripts on this box, 251 are
#: loops.
#:
#: WHY os.environ AND NOT config.py: nobody on this programme owns
#: config.py, so the setting is read here rather than added there. The diff
#: that belongs in `Settings.__init__` is in this change's report; when it
#: lands, this function becomes `settings.video_ocr_prompt` and the default
#: below moves with it. VIDEO_OCR_PROMPT is read per call so an operator can
#: change it in the environment of a restarted container without a rebuild.
_DEFAULT_VIDEO_OCR_PROMPT = "OCR"


def video_ocr_prompt() -> str:
    """The prompt this deployment sends the OCR engine for a frame."""
    return (os.environ.get("VIDEO_OCR_PROMPT") or "").strip() or _DEFAULT_VIDEO_OCR_PROMPT


@dataclass(frozen=True)
class ScreenRead:
    """The OCR stage's answer for one video: per frame, and as a whole.

    `texts` is what the rest of the pipeline indexes and shows — '' unless
    the frame was actually READ, so a failed or looping read never reaches
    the evidence pack as on-screen text. `reads` carries the status of each
    frame, and `summary` is what the analysis row and fusion need in order to
    say "the reader was unavailable" rather than "the screen was blank".
    """

    texts: List[str]
    reads: List[Any] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return str(self.summary.get("status") or "ok")

    @property
    def unavailable(self) -> bool:
        """Nothing was read, and not because there was nothing to read."""
        return self.status == "unavailable"


def _summarise(reads: Sequence, *, frames: int, prompt: str, note: str = "") -> Dict[str, Any]:
    """Per-frame statuses -> the stage-level summary, in one place."""
    counts = {"ok": 0, "empty": 0, "degenerate": 0, "failed": 0}
    for read in reads:
        counts[read.status] = counts.get(read.status, 0) + 1
    unread = counts["degenerate"] + counts["failed"]
    if frames == 0:
        status = "skipped"
    elif unread == 0:
        status = "ok"
    elif counts["ok"] + counts["empty"] == 0:
        status = "unavailable"
    else:
        status = "partial"
    detail = f"{counts['ok']}/{frames} frames had readable text"
    if counts["degenerate"]:
        detail += f" · {counts['degenerate']} unreadable (the reader looped)"
    if counts["failed"]:
        detail += f" · {counts['failed']} not read"
    return {
        "status": status,
        "frames": frames,
        "prompt": prompt,
        "detail": detail + note,
        **counts,
        "unread": unread,
        "errors": sorted({r.error for r in reads if r.error})[:3],
    }


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


async def read_frames(
    frames: Sequence[KeptFrame], *, progress: Progress
) -> ScreenRead:
    """Read every frame, and say for each one what happened.

    A batch that fails, times out or comes back as a loop is recorded as a
    FAILED read of those frames — never as frames with nothing on them — so
    the pipeline can decline to stamp the stage done and cache an outage for
    the life of those bytes.
    """
    from ..engines import ocr

    prompt = video_ocr_prompt()
    if not frames:
        return ScreenRead([], [], _summarise([], frames=0, prompt=prompt))
    if not settings.ocr_enabled or not settings.video_ocr_enabled:
        reads = [ocr.OcrRead("", "failed", "the on-screen text reader is turned off") for _ in frames]
        summary = _summarise(reads, frames=len(frames), prompt=prompt)
        summary["status"] = "disabled"
        summary["detail"] = "OCR is not enabled"
        return ScreenRead(["" for _ in frames], reads, summary)
    batch = max(1, settings.video_ocr_concurrency)
    texts: List[str] = []
    reads: List[Any] = []
    from . import pipeline as _pipe

    for i in range(0, len(frames), batch):
        await _pipe.pace()
        chunk = frames[i : i + batch]
        urls = await asyncio.to_thread(lambda c=chunk: [_data_url(f.path) for f in c])
        batch_reads = await ocr.read_images(
            urls,
            prompt=prompt,
            max_output_tokens=settings.video_ocr_max_tokens,
            deadline_s=settings.video_ocr_batch_deadline_s,
        )
        for read in batch_reads:
            # Only a read that succeeded becomes on-screen text. A loop is
            # kept in `reads` for the record and dropped from the evidence.
            texts.append(_clean_ocr(read.text) if read.ok else "")
        reads.extend(batch_reads)
        await progress(100.0 * len(texts) / len(frames), f"{len(texts)}/{len(frames)} frames")
    summary = _summarise(reads, frames=len(frames), prompt=prompt)
    if summary["status"] in ("unavailable", "partial"):
        log.warning(
            "video OCR: %s (prompt %r) — %s", summary["status"], prompt, summary["detail"]
        )
    return ScreenRead(texts, reads, summary)


async def ocr_frames(
    frames: Sequence[KeptFrame], *, progress: Progress
) -> List[str]:
    """One OCR transcript per frame ('' when nothing legible / failed).

    The flattened view of `read_frames`, for the caller that does not (yet)
    carry the per-frame status.
    """
    return (await read_frames(frames, progress=progress)).texts


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


def caption_summary(captions: Sequence[Optional[str]], *, enabled: bool) -> Dict[str, Any]:
    """The captioner's stage-level summary, in the shape `_summarise` uses.

    Captions have always been allowed to fail one frame at a time, but the
    number that failed was only ever a percentage in a progress line. Fusion
    needs it as a fact so a video whose frames were never described is not
    summarised as a video with nothing worth describing.
    """
    frames = len(captions)
    described = sum(1 for c in captions if c)
    if not enabled:
        status, detail = "disabled", "frame captions are not enabled"
    elif frames == 0:
        status, detail = "skipped", "no frames to describe"
    elif described == 0:
        status, detail = "unavailable", "the vision model described none of the frames"
    elif described < frames:
        status, detail = "partial", f"{described}/{frames} frames described"
    else:
        status, detail = "ok", f"{described}/{frames} frames described"
    return {"status": status, "frames": frames, "described": described, "detail": detail}


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
