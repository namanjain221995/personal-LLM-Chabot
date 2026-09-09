"""Speech + screen -> understanding. The main model reads the evidence pack.

THE EVIDENCE PACK is one plain-text timeline the model can cite from:

    [0:00-0:31] SPEECH: Good morning, everyone …
    [0:00-0:31] SCREEN (slide): Weekly Planning Meeting / Agenda …
    [0:31-1:21] SPEECH: …

Speech is grouped into blocks of about a minute so a two-hour meeting is a
few hundred lines rather than three thousand; screen spans keep their own
boundaries. Every line carries the timestamp the summary will cite.

SIZED AGAINST THE SERVED WINDOW, WITH HEADROOM. The model serves a
1,000,000-token window on this deployment, but a prompt that large
monopolises the engine (1.49x concurrency at full window), so fusion is
direct only while the pack is under `VIDEO_FUSION_DIRECT_TOKENS` (60k by
default). Above that it is MAP-REDUCE: parts of `VIDEO_FUSION_PART_TOKENS`
are each summarised into structured notes, and the notes are fused. Chapter
boundaries come from the parts' own chapter proposals, so a two-hour
recording gets chapters with real timestamps rather than one paragraph.

NOTHING IS INVENTED. The prompt says so, the schema forces `not_covered`,
and every chapter start is checked to lie inside the video. A model that
returns a timestamp past the end has hallucinated and the chapter is
dropped rather than trusted.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional, Sequence, Tuple

from ..config import settings
from .artifacts import fmt_ts, parse_ts
from .types import CONTENT_TYPES, Chapter, OcrSpan, Segment, Understanding

log = logging.getLogger(__name__)

#: Speech is grouped into blocks no longer than this, cut at segment ends.
_BLOCK_S = 60.0
_BLOCK_CHARS = 900


def _estimate_tokens(text: str) -> int:
    from .. import context

    return context.estimate_tokens(text)


# ------------------------------------------------------------ the pack --


def speech_blocks(segments: Sequence[Segment]) -> List[Tuple[float, float, str]]:
    """Segments -> (start, end, text) blocks of roughly a minute."""
    blocks: List[Tuple[float, float, str]] = []
    cur: List[Segment] = []
    for s in segments:
        if not s.text.strip():
            continue
        if cur and (
            s.end_s - cur[0].start_s > _BLOCK_S
            or sum(len(c.text) + 1 for c in cur) + len(s.text) > _BLOCK_CHARS
        ):
            blocks.append((cur[0].start_s, cur[-1].end_s, " ".join(c.text.strip() for c in cur)))
            cur = []
        cur.append(s)
    if cur:
        blocks.append((cur[0].start_s, cur[-1].end_s, " ".join(c.text.strip() for c in cur)))
    return blocks


def evidence_lines(
    segments: Sequence[Segment],
    spans: Sequence[OcrSpan],
    *,
    screen_chars: int = 700,
) -> List[Tuple[float, str]]:
    """The merged, time-ordered timeline. Each entry is (start_s, line)."""
    lines: List[Tuple[float, str]] = []
    for start, end, text in speech_blocks(segments):
        lines.append((start, f"[{fmt_ts(start)}-{fmt_ts(end)}] SPEECH: {text}"))
    for span in spans:
        label = f"SCREEN ({span.kind})" if span.kind else "SCREEN"
        body = " / ".join(ln.strip() for ln in span.text.splitlines() if ln.strip())
        if len(body) > screen_chars:
            body = body[:screen_chars].rstrip() + " …"
        parts = []
        if span.caption:
            parts.append(span.caption.strip())
        if body:
            parts.append("TEXT: " + body)
        if not parts:
            parts.append("nothing legible on screen")
        lines.append((span.start_s, f"[{fmt_ts(span.start_s)}-{fmt_ts(span.end_s)}] {label}: {' — '.join(parts)}"))
    lines.sort(key=lambda t: t[0])
    return lines


def render_pack(lines: Sequence[Tuple[float, str]]) -> str:
    return "\n".join(line for _t, line in lines)


def split_pack(lines: Sequence[Tuple[float, str]], part_tokens: int) -> List[List[Tuple[float, str]]]:
    """Cut the timeline into parts under `part_tokens`, never mid-line."""
    parts: List[List[Tuple[float, str]]] = []
    cur: List[Tuple[float, str]] = []
    used = 0
    for entry in lines:
        cost = _estimate_tokens(entry[1]) + 1
        if cur and used + cost > part_tokens:
            parts.append(cur)
            cur, used = [], 0
        cur.append(entry)
        used += cost
    if cur:
        parts.append(cur)
    return parts


# ---------------------------------------------------------------- schema --

_CHAPTER_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "timestamp like 12:34 or 1:02:03 taken from the evidence"},
        "end": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["start", "title"],
}

_NOTES_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "chapters": {"type": "array", "items": _CHAPTER_SCHEMA},
        "content_type": {"type": "string", "enum": list(CONTENT_TYPES)},
        "not_covered": {"type": "string"},
    },
    "required": ["summary", "key_points", "decisions", "action_items", "entities", "chapters", "content_type", "not_covered"],
}

_RULES = (
    "You are analysing a video from its EVIDENCE: a timestamped speech transcript "
    "(SPEECH lines) and timestamped descriptions of what was on screen (SCREEN "
    "lines). Rules:\n"
    "1. State only what the evidence supports. Do not infer decisions, names, "
    "numbers or outcomes that are not in it. If the evidence does not say, leave "
    "the field empty and say what is missing in `not_covered`.\n"
    "2. Every chapter `start` must be a timestamp that appears in the evidence.\n"
    "3. `decisions` are things explicitly agreed or decided; `action_items` are "
    "tasks with an owner or deadline when stated; `entities` are people, "
    "organisations, products and technical terms that actually appear.\n"
    "4. Keep speech and screen distinct in your mind: something written on a "
    "slide was shown, not necessarily said.\n"
    "5. Write in the language most of the speech is in; keep names and technical "
    "terms as they appear."
)


def _as_notes(raw: str) -> Optional[dict]:
    """Parse the model's JSON, tolerating fences and a stray prefix."""
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            text = text[first : last + 1]
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _chapters_from(data: dict, *, duration_s: float) -> List[Chapter]:
    out: List[Chapter] = []
    for item in data.get("chapters") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = parse_ts(str(item.get("start", "")))
        except ValueError:
            continue
        if start < 0 or start > duration_s + 1:
            continue  # a timestamp past the end is invented
        end = duration_s
        try:
            end = min(duration_s, max(start, parse_ts(str(item.get("end", "")))))
        except ValueError:
            pass
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        out.append(Chapter(start_s=float(start), end_s=float(end), title=title[:160], summary=str(item.get("summary") or "").strip()[:600]))
    out.sort(key=lambda c: c.start_s)
    # Two chapters five seconds apart are one chapter: keep the first.
    kept: List[Chapter] = []
    for c in out:
        if kept and c.start_s - kept[-1].start_s < 5.0:
            continue
        kept.append(c)
    # Chapters do not overlap; each ends where the NEXT KEPT one begins — the
    # ends are assigned after the filter, or a dropped chapter's start would
    # cut its predecessor short.
    fixed: List[Chapter] = []
    for i, c in enumerate(kept):
        end = kept[i + 1].start_s if i + 1 < len(kept) else duration_s
        fixed.append(Chapter(c.start_s, max(c.start_s, end), c.title, c.summary))
    return fixed


def _understanding_from(data: dict, *, duration_s: float, method: str) -> Understanding:
    content_type = str(data.get("content_type") or "other").strip().lower().replace(" ", "_")
    if content_type not in CONTENT_TYPES:
        content_type = "other"
    u = Understanding.from_json(
        {
            **data,
            "content_type": content_type,
            "chapters": [],
        }
    )
    u.chapters = _chapters_from(data, duration_s=duration_s)
    u.method = method
    return u


# ------------------------------------------------------------- the calls --


async def _ask(system: str, user: str, *, max_tokens: int) -> Optional[dict]:
    from .. import llm

    raw = await llm.json_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        json_schema=_NOTES_SCHEMA,
        schema_name="video_understanding",
        temperature=0.1,
        max_tokens=max_tokens,
        thinking=False,
    )
    data = _as_notes(raw)
    if data is None:
        # One repair pass: the unconstrained fallback can wrap or truncate.
        raw = await llm.json_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user + "\n\nReturn ONLY the JSON object."},
            ],
            json_schema=_NOTES_SCHEMA,
            schema_name="video_understanding",
            temperature=0.0,
            max_tokens=max_tokens,
            thinking=False,
        )
        data = _as_notes(raw)
    return data


def _header(
    *,
    filename: str,
    duration_s: float,
    language: Optional[str],
    has_audio: bool,
    speech_fraction: Optional[float],
    has_speech: bool = True,
) -> str:
    bits = [f"File: {filename}", f"Duration: {fmt_ts(duration_s)}"]
    if language:
        bits.append(f"Speech language: {language}")
    if not has_audio:
        bits.append("The file has NO audio track; the evidence is on-screen only.")
    elif (speech_fraction is not None and speech_fraction < 0.02) or not has_speech:
        # Said plainly so the model reports "no speech" rather than
        # describing the shape of the evidence it was given.
        bits.append("The audio track contains no transcribable speech; the evidence is on-screen only.")
    return "\n".join(bits)


async def understand(
    *,
    filename: str,
    duration_s: float,
    language: Optional[str],
    has_audio: bool,
    speech_fraction: Optional[float],
    segments: Sequence[Segment],
    spans: Sequence[OcrSpan],
    progress,
) -> Understanding:
    """The whole thing: evidence -> Understanding, direct or map-reduce."""
    lines = evidence_lines(segments, spans)
    if not lines:
        return Understanding(
            content_type="other",
            summary="",
            not_covered="The video produced no speech transcript and no readable on-screen content, so there is nothing to summarise.",
            method="empty",
        )
    header = _header(
        filename=filename,
        duration_s=duration_s,
        language=language,
        has_audio=has_audio,
        speech_fraction=speech_fraction,
        has_speech=any(s.text.strip() for s in segments),
    )
    pack = render_pack(lines)
    total_tokens = _estimate_tokens(pack)
    direct_limit = max(8_000, settings.video_fusion_direct_tokens)

    if total_tokens <= direct_limit:
        await progress(10.0, f"{total_tokens:,} tokens of evidence, one pass")
        user = (
            f"{header}\n\nEVIDENCE:\n{pack}\n\n"
            "Produce the JSON: an executive `summary` (5-10 sentences), `chapters` "
            "with real timestamps covering the whole video, `key_points`, "
            "`decisions`, `action_items`, `entities`, the `content_type`, and "
            "`not_covered`."
        )
        data = await _ask(_RULES, user, max_tokens=settings.video_fusion_max_tokens)
        if data is None:
            raise RuntimeError("the model did not return a readable understanding")
        await progress(100.0, "done")
        return _understanding_from(data, duration_s=duration_s, method="direct")

    # Map: notes per part.
    parts = split_pack(lines, max(4_000, settings.video_fusion_part_tokens))
    notes: List[dict] = []
    for i, part in enumerate(parts):
        span_start, span_end = part[0][0], part[-1][0]
        await progress(5.0 + 80.0 * i / len(parts), f"part {i + 1}/{len(parts)} ({fmt_ts(span_start)}-{fmt_ts(span_end)})")
        user = (
            f"{header}\nThis is PART {i + 1} of {len(parts)}, covering roughly "
            f"{fmt_ts(span_start)} to {fmt_ts(span_end)}.\n\nEVIDENCE:\n{render_pack(part)}\n\n"
            "Produce the JSON for THIS PART ONLY: a `summary` (3-6 sentences), "
            "`chapters` for this part with real timestamps, `key_points`, "
            "`decisions`, `action_items`, `entities`, your `content_type` guess, "
            "and `not_covered`."
        )
        data = await _ask(_RULES, user, max_tokens=settings.video_fusion_max_tokens)
        if data is not None:
            data["_range"] = [span_start, span_end]
            notes.append(data)
    if not notes:
        raise RuntimeError("no part of the evidence produced notes")

    # Reduce: the notes, in order, into one understanding.
    await progress(88.0, f"fusing {len(notes)} part(s)")
    rendered = []
    for i, n in enumerate(notes):
        rng = n.get("_range") or [0, 0]
        rendered.append(
            f"--- PART {i + 1} ({fmt_ts(rng[0])}-{fmt_ts(rng[1])}) ---\n"
            + json.dumps({k: v for k, v in n.items() if k != "_range"}, ensure_ascii=False, indent=1)
        )
    user = (
        f"{header}\n\nThe video was analysed in {len(notes)} parts; here are the "
        "notes for each part, in order. Fuse them into ONE understanding of the "
        "whole video: an executive `summary` (6-12 sentences), `chapters` "
        "covering the whole video (merge the parts' chapters; keep their "
        "timestamps exactly), deduplicated `key_points`, `decisions`, "
        "`action_items`, `entities`, the overall `content_type`, and "
        "`not_covered`.\n\n" + "\n\n".join(rendered)
    )
    data = await _ask(_RULES, user, max_tokens=settings.video_fusion_max_tokens)
    if data is None:
        # Degrade honestly: concatenate the parts rather than fail the video.
        merged: dict = {
            "summary": " ".join(str(n.get("summary") or "") for n in notes),
            "key_points": [p for n in notes for p in (n.get("key_points") or [])],
            "decisions": [p for n in notes for p in (n.get("decisions") or [])],
            "action_items": [p for n in notes for p in (n.get("action_items") or [])],
            "entities": sorted({str(e) for n in notes for e in (n.get("entities") or [])}),
            "chapters": [c for n in notes for c in (n.get("chapters") or [])],
            "content_type": notes[0].get("content_type") or "other",
            "not_covered": " ".join(str(n.get("not_covered") or "") for n in notes),
        }
        data = merged
    await progress(100.0, "done")
    return _understanding_from(data, duration_s=duration_s, method=f"map_reduce:{len(notes)}")
