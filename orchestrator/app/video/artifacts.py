"""The files a person can download: transcript in four formats, screen text,
and the understanding as Markdown.

Formats are written by hand rather than through a subtitle library because
each is a few lines and the two subtitle ones have rules worth stating:

* SRT cues are numbered from 1, use `,` in the timestamp, and a cue must not
  be empty or a player skips the whole file.
* WebVTT needs the `WEBVTT` header line, uses `.` in the timestamp, and
  forbids `-->` inside cue text.
* Both need `start < end`; a zero-length cue is invisible in most players.
  `_cue_end` guarantees a minimum length.

Every timestamp in every artifact comes from the same `fmt_ts` so that what a
person reads in the chat, in transcript.txt and in the JSON agree to the
second.
"""
from __future__ import annotations

import json
from typing import Iterable, List, Sequence

from .types import OcrSpan, Segment, Understanding

#: A cue shorter than this is stretched: players do not render a 0 ms cue.
_MIN_CUE_S = 0.4


def fmt_ts(seconds: float, *, hours_always: bool = False) -> str:
    """`m:ss` under an hour, `h:mm:ss` above it — the shape used in citations."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h or hours_always:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def parse_ts(text: str) -> float:
    """`1:02:03`, `12:34` or `45` -> seconds. Raises ValueError on junk."""
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"not a timestamp: {text!r}")
    nums = [int(p) for p in parts]
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, s = nums
    return h * 3600 + m * 60 + s


def _srt_ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_ts(seconds: float) -> str:
    return _srt_ts(seconds).replace(",", ".")


def _cue_end(seg: Segment, following: Segment | None) -> float:
    end = max(seg.end_s, seg.start_s + _MIN_CUE_S)
    if following is not None:
        end = min(end, max(following.start_s, seg.start_s + _MIN_CUE_S))
    return end


def transcript_txt(segments: Sequence[Segment], *, title: str = "") -> str:
    lines: List[str] = []
    if title:
        lines.append(title)
        lines.append("=" * min(len(title), 72))
        lines.append("")
    for seg in segments:
        text = seg.text.strip()
        if text:
            lines.append(f"[{fmt_ts(seg.start_s)}] {text}")
    return "\n".join(lines) + ("\n" if lines else "")


def transcript_srt(segments: Sequence[Segment]) -> str:
    out: List[str] = []
    n = 0
    segs = [s for s in segments if s.text.strip()]
    for i, seg in enumerate(segs):
        n += 1
        end = _cue_end(seg, segs[i + 1] if i + 1 < len(segs) else None)
        out.append(f"{n}\n{_srt_ts(seg.start_s)} --> {_srt_ts(end)}\n{seg.text.strip()}\n")
    return "\n".join(out)


def transcript_vtt(segments: Sequence[Segment]) -> str:
    out: List[str] = ["WEBVTT", ""]
    segs = [s for s in segments if s.text.strip()]
    for i, seg in enumerate(segs):
        end = _cue_end(seg, segs[i + 1] if i + 1 < len(segs) else None)
        text = seg.text.strip().replace("-->", "→")
        out.append(f"{_vtt_ts(seg.start_s)} --> {_vtt_ts(end)}\n{text}\n")
    return "\n".join(out)


def transcript_json(
    segments: Sequence[Segment], *, language: str | None, duration_s: float, extra: dict | None = None
) -> str:
    payload = {
        "language": language,
        "duration_s": round(duration_s, 3),
        "segments": [
            {
                "start": round(s.start_s, 3),
                "end": round(s.end_s, 3),
                "text": s.text.strip(),
                "language": s.language,
            }
            for s in segments
            if s.text.strip()
        ],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False, indent=1)


def screen_text_txt(spans: Sequence[OcrSpan]) -> str:
    lines: List[str] = []
    for span in spans:
        text = span.text.strip()
        if not text:
            continue
        lines.append(f"[{fmt_ts(span.start_s)} - {fmt_ts(span.end_s)}]")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def screen_text_json(spans: Sequence[OcrSpan]) -> str:
    return json.dumps(
        [
            {
                "start": round(s.start_s, 3),
                "end": round(s.end_s, 3),
                "text": s.text.strip(),
                "kind": s.kind,
                "caption": s.caption,
            }
            for s in spans
            if s.text.strip() or s.caption
        ],
        ensure_ascii=False,
        indent=1,
    )


def summary_md(u: Understanding, *, title: str, duration_s: float, language: str | None) -> str:
    """The understanding as a document a person can keep."""
    out: List[str] = [f"# {title}", ""]
    meta = [f"Duration: {fmt_ts(duration_s)}"]
    if language:
        meta.append(f"Language: {language}")
    if u.content_type:
        meta.append(f"Type: {u.content_type}")
    out.append(" · ".join(meta))
    out.append("")
    if u.summary:
        out += ["## Summary", "", u.summary.strip(), ""]
    if u.chapters:
        out += ["## Chapters", ""]
        for ch in u.chapters:
            line = f"- **[{fmt_ts(ch.start_s)}]** {ch.title.strip()}"
            if ch.summary:
                line += f" — {ch.summary.strip()}"
            out.append(line)
        out.append("")
    for heading, items in (
        ("Key points", u.key_points),
        ("Decisions", u.decisions),
        ("Action items", u.action_items),
    ):
        if items:
            out += [f"## {heading}", ""]
            out += [f"- {item.strip()}" for item in items if item.strip()]
            out.append("")
    if u.entities:
        out += ["## People, organisations and terms", ""]
        out += [f"- {e.strip()}" for e in u.entities if e.strip()]
        out.append("")
    if u.limitations:
        # Above "Not covered" on purpose: what the analysis could not SEE is
        # a different claim from what the video did not cover, and a reader
        # who stops after the summary must still meet it.
        out += ["## What this analysis could not see", ""]
        out += [f"- {lim.sentence.strip()}" for lim in u.limitations if lim.sentence.strip()]
        out.append("")
    if u.not_covered:
        out += ["## Not covered", "", u.not_covered.strip(), ""]
    return "\n".join(out)


def write_text(path: str, text: str) -> int:
    """Write atomically; return bytes written."""
    import os

    tmp = path + ".part"
    data = text.encode("utf-8")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return len(data)


def iter_lines(text: str) -> Iterable[str]:
    for line in text.splitlines():
        if line.strip():
            yield line
