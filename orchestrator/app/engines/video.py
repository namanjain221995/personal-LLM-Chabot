"""The video engine: what the assistant says about a video, and how it waits.

TWO KINDS OF TURN.

* The ATTACH turn — the message that carried the video. If the person asked
  nothing in particular, the answer is the understanding itself, rendered
  straight from the analysis: summary, chapters with timestamps, key points,
  decisions, action items, people and terms, and what the video does not
  cover. No model call: the fusion stage already did the thinking, and a
  second pass over the same evidence would only add a chance to drift.
  If they asked something, it is answered as below.

* A QUESTION — on the attach turn or forty minutes later. Evidence is
  retrieved from the index (speech, on-screen text, frame descriptions —
  the reranker orders them with the question in hand), the summary and
  chapters always ride along, and when the question is VISUAL ("read the
  code on the slide", "what is on screen at 14:22") the frames nearest the
  timestamps in play go to the main model as images, because it can see.
  Every claim carries a `[m:ss]` citation; when the evidence does not hold
  the answer, the model is told to say so, and the prompt shows it what
  "not covered" looks like.

WAITING. The analysis is a detached job (video/pipeline.py). If it is still
running when the turn arrives, the engine subscribes, forwards each stage as
a `step` event — title, `43% · 12:30 of 35:00 · 71s` — and waits. Stage
ids are fixed (probe=1 … artifacts=9), so a reload that re-attaches to the
generation sees the same timeline, and the frontend's AgentTimeline renders
it with its own elapsed clock. Progress is coalesced: one event per stage
transition, and at most one every few seconds while a stage runs.

ARTIFACTS ride on `meta.report_files`, which main.py binds to the viewer and
GET /reports/{filename} serves. `report_files.filename` is a global primary
key bound to whoever first advertised it, so the files are copied under a
per-user name — the same video attached by two people is two sets of files,
each downloadable by its owner.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import llm
from ..config import settings
from ..video import artifacts as art
from ..video import index as video_index
from ..video import pipeline, store
from ..video.types import STAGE_TITLES, STAGES, Understanding
from . import recent_turns
from .vision import VISION_ANSWER_TOKENS

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: The composer's placeholder for a send with a video and no words (kept in
#: step with frontend/lib/orchestrator.ts VIDEO_ONLY_PROMPT).
VIDEO_ONLY_PROMPT = "Analyze the attached video."

_STEP_IDS: Dict[str, int] = {name: i + 1 for i, name in enumerate(STAGES)}

_TIMESTAMP_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")
_VISUAL_RE = re.compile(
    r"\b(slide|screen|on[- ]screen|shown|showing|show|display|code|terminal|diagram|"
    r"chart|graph|table|picture|image|frame|visible|see|look|read|written|text on|"
    r"whiteboard|demo|ui|button|window|error message)\b",
    re.I,
)
_ABOUT_VIDEO_RE = re.compile(
    r"\b(video|recording|clip|transcript|meeting|call|lecture|webinar|session|talk|"
    r"speaker|presenter|narrator|host|she said|he said|they said|said about|say about|"
    r"\bsays?\b|\bsaid\b|\bspoke\b|\bspeaks?\b|\btalked\b|discussed|mentioned|slide|screen|"
    r"chapter|timestamp|minute|decided|decision|action item|summary|summarise|summarize|"
    r"recap|what happened|when do|when did|at \d{1,2}:\d{2}|tutorial|lesson|course|"
    r"webinar|demo|presentation|walkthrough|walks? through|explain(?:s|ed)?|"
    r"application|the app\b|on the video|in the video|this one)\b",
    re.I,
)

_SYSTEM = (
    "You are answering questions about video(s) the user attached, using ONLY the "
    "evidence provided: a summary, chapters, and retrieved passages of what was SAID "
    "(SPEECH) and what was ON SCREEN (SCREEN), each with a timestamp range, plus any "
    "frames attached as images.\n"
    "Rules:\n"
    "1. Cite timestamps for every claim, in square brackets, using the times from the "
    "evidence, e.g. [12:34] or [1:02:15]. Cite the start of the range.\n"
    "2. If the evidence does not contain the answer, say so plainly — for example: "
    "\"The video doesn't cover pricing.\" Do not guess, and never invent a decision, a "
    "name, a number or a quote. A wrong meeting decision is worse than no answer.\n"
    "3. Quote on-screen text verbatim when asked to read something; say it came from "
    "the screen, not from speech, when that matters.\n"
    "4. When more than one video is attached, say which one you are citing.\n"
    "5. Answer in the language of the question. Be direct; use short sections or "
    "bullets only when the answer has several parts."
)


# ------------------------------------------------------------ helpers --


def _mmss(seconds: float) -> str:
    return art.fmt_ts(seconds)


def _duration_s(row: dict) -> float:
    return float(row.get("duration_ms") or 0) / 1000.0


def _display_name(row: dict) -> str:
    return str(row.get("display_name") or row.get("filename") or "video")


def is_placeholder(message: str) -> bool:
    text = (message or "").strip().rstrip(".").lower()
    return not text or text == VIDEO_ONLY_PROMPT.rstrip(".").lower()


def mentioned_times(message: str) -> List[float]:
    out: List[float] = []
    for m in _TIMESTAMP_RE.finditer(message or ""):
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is not None:
            out.append(int(a) * 3600 + int(b) * 60 + int(c))
        else:
            out.append(int(a) * 60 + int(b))
    return out


def is_visual(message: str) -> bool:
    return bool(_VISUAL_RE.search(message or ""))


async def is_about_video(message: str, videos: Sequence[dict]) -> bool:
    """Should a text-only turn in a conversation with videos go to this engine?

    Two tests, either suffices: the message reads like it is about a
    recording (cue words), or the nearest indexed chunk is close enough that
    the index plainly has something to say about it. The second is one
    embedding call (~10 ms) and degrades to False when embeddings are down —
    the chat engine still gets the pinned summary block, so nothing is lost
    but the citations.
    """
    if not videos:
        return False
    if _ABOUT_VIDEO_RE.search(message or ""):
        return True
    ids = [int(v["id"]) for v in videos if v.get("status") == "done"]
    if not ids:
        return False
    distance = await video_index.best_distance(message, ids)
    return distance is not None and distance <= settings.video_followup_distance


def pinned_block(videos: Sequence[dict], *, max_chars: int) -> str:
    """The compact block every later turn carries: what each video is."""
    parts: List[str] = []
    budget = max(1000, max_chars)
    per = max(600, budget // max(1, len(videos)))
    for row in videos:
        u = Understanding.from_json(row.get("understanding") or {})
        head = f"{_display_name(row)} ({_mmss(_duration_s(row))}"
        if u.content_type and u.content_type != "other":
            head += f", {u.content_type.replace('_', ' ')}"
        if row.get("language"):
            head += f", {row['language']}"
        head += ")"
        if row.get("status") != "done":
            parts.append(f"{head} — analysis {row.get('status')}{': ' + row['error'] if row.get('error') else ''}")
            continue
        body = [head]
        if u.summary:
            body.append("Summary: " + u.summary.strip())
        if u.chapters:
            body.append("Chapters: " + "; ".join(f"[{_mmss(c.start_s)}] {c.title}" for c in u.chapters[:24]))
        if u.decisions:
            body.append("Decisions: " + "; ".join(u.decisions[:8]))
        text = "\n".join(body)
        if len(text) > per:
            text = text[: per - 1].rstrip() + "…"
        parts.append(text)
    return "\n\n".join(parts)


def overview_markdown(row: dict) -> str:
    """The attach-turn answer, rendered from the analysis without a model."""
    u = Understanding.from_json(row.get("understanding") or {})
    name = _display_name(row)
    duration = _duration_s(row)
    counts = row.get("counts") or {}
    head = [f"**{name}** — {_mmss(duration)}"]
    if u.content_type and u.content_type != "other":
        head[0] += f" · {u.content_type.replace('_', ' ')}"
    if row.get("language"):
        head[0] += f" · {row['language']}"
    facts = []
    if row.get("has_audio") is False:
        facts.append("no audio track")
    elif not counts.get("segments"):
        facts.append("no speech detected")
    if counts.get("frames_kept"):
        facts.append(f"{counts['frames_kept']} distinct frames read")
    if facts:
        head.append("_" + " · ".join(facts) + "_")
    out: List[str] = ["\n".join(head), ""]
    if u.summary:
        out += ["## Summary", "", u.summary.strip(), ""]
    if u.chapters:
        out += ["## Chapters", ""]
        for c in u.chapters:
            line = f"- **[{_mmss(c.start_s)}]** {c.title}"
            if c.summary:
                line += f" — {c.summary}"
            out.append(line)
        out.append("")
    for heading, items in (("Key points", u.key_points), ("Decisions", u.decisions), ("Action items", u.action_items)):
        if items:
            out += [f"## {heading}", ""] + [f"- {i}" for i in items] + [""]
    if u.entities:
        out += ["## People, organisations and terms", "", ", ".join(u.entities), ""]
    if u.not_covered:
        out += ["## Not covered", "", u.not_covered.strip(), ""]
    if not u.summary and not u.chapters:
        out += [
            "The analysis finished but found nothing to summarise: "
            + (u.not_covered or "no speech was transcribed and nothing legible was on screen."),
            "",
        ]
    out.append("_Ask anything about it — what was said, what was on screen, when something happened — and I'll cite the timestamps. The transcript files are attached below._")
    return "\n".join(out)


# ----------------------------------------------------------- waiting --


def _status_line(running: Dict[str, Optional[float]]) -> str:
    """'Transcribing 40% · Reading on-screen text 3/10…' — every stage in
    flight, in pipeline order, so the two branches read as one job."""
    parts = []
    for stage in STAGES:
        if stage in running:
            pct = running[stage]
            parts.append(f"{STAGE_TITLES[stage]}{' ' + f'{pct:.0f}%' if pct is not None else ''}")
    return (" · ".join(parts) or "Analysing the video") + "…"


async def _wait_for_analysis(row: dict, emit: Emit) -> dict:
    """Forward the job's progress as steps until it finishes; return the row."""
    analysis_id = int(row["id"])
    if row.get("status") == "done":
        return row
    # Subscribe FIRST: a job that starts (or is already running) publishes
    # immediately, and only the last event is replayed to a late subscriber.
    queue = pipeline.subscribe(analysis_id)
    await pipeline.ensure_running(analysis_id)
    last_sent: Dict[str, Tuple[float, Optional[float], str]] = {}
    # What is running right now, by stage: the status line names all of it,
    # because speech and screen are analysed side by side and a line that
    # flipped between them every second would read as two jobs fighting.
    running: Dict[str, Optional[float]] = {}
    announced_remote = False
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                if pipeline.is_running(analysis_id):
                    continue
                fresh = await pipeline.wait_for(analysis_id)
                if fresh is None or fresh.get("status") not in ("queued", "running"):
                    return fresh if fresh is not None else row
                # V29: the row is in flight but not in THIS process — another
                # process holds its lease (a rolling recreate, a second
                # orchestrator), so its progress is not ours to forward.
                # Say so once, keep waiting, and re-ask the pipeline each
                # round so this process claims the run the moment that
                # lease lapses instead of answering from a row still marked
                # 'running'.
                if not announced_remote:
                    await emit("status", {"text": "The video is being analysed by another worker…"})
                    announced_remote = True
                await pipeline.ensure_running(analysis_id)
                continue
            stage = str(event.get("stage") or "")
            status = str(event.get("status") or "")
            if stage == "_done":
                break
            if stage not in _STEP_IDS:
                continue
            percent = event.get("percent")
            detail = str(event.get("detail") or "")
            elapsed = float(event.get("elapsed_s") or 0.0)
            now = time.monotonic()
            prev = last_sent.get(stage)
            if status == "running" and prev is not None:
                prev_t, prev_pct, prev_status = prev
                moved = (percent is not None and prev_pct is not None and abs(percent - prev_pct) >= 5) or percent is None or prev_pct is None
                if prev_status == "running" and now - prev_t < 3.0 and not moved:
                    continue
            step_status = "running" if status == "running" else ("failed" if status == "failed" else "done")
            bits = []
            if status == "running" and percent is not None:
                bits.append(f"{percent:.0f}%")
            if detail:
                bits.append(detail)
            if status == "running" and elapsed >= 1:
                bits.append(f"{elapsed:.0f}s")
            if status == "skipped":
                bits.insert(0, "skipped")
            elif event.get("cached"):
                bits.insert(0, "cached")
            await emit("step", {"id": _STEP_IDS[stage], "title": STAGE_TITLES[stage], "status": step_status, "detail": " · ".join(bits)})
            if status == "running":
                running[stage] = percent if isinstance(percent, (int, float)) else None
            else:
                running.pop(stage, None)
            if status == "running":
                await emit("status", {"text": _status_line(running)})
            last_sent[stage] = (now, percent if isinstance(percent, (int, float)) else None, status)
    finally:
        pipeline.unsubscribe(analysis_id, queue)
    fresh = await pipeline.wait_for(analysis_id)
    return fresh or row


# ------------------------------------------------------------ evidence --


async def _frame_exactly_at(row: dict, t_s: float) -> Optional[Tuple[str, float]]:
    """Decode the frame AT `t_s` from the source — "what is on screen at 4:00"
    deserves the picture at 4:00, not the nearest one the periodic floor kept
    four seconds earlier. Falls back to the kept frame when the source is
    gone (it is hard-linked into the analysis directory, so that is rare) or
    when the decode fails."""
    from ..video import media

    source = store.source_path(row["content_hash"])
    if not source:
        return _frame_at(row, t_s)
    out = os.path.join(store.frames_dir(row["content_hash"]), f"at_{int(round(t_s * 1000)):09d}.jpg")
    if not os.path.exists(out):
        try:
            await media.render_frame(source, t_s, out, max_width=settings.video_frame_max_width, timeout_s=60.0)
        except Exception:  # noqa: BLE001 — the kept frame is the fallback
            log.warning("could not render the frame at %.1fs", t_s, exc_info=True)
            return _frame_at(row, t_s)
    return (out, float(t_s))


def _frame_at(row: dict, t_s: float) -> Optional[Tuple[str, float]]:
    """The kept frame whose span contains t (or the nearest one)."""
    data = store.read_json(store.stage_path(row["content_hash"], "frames.json")) or {}
    frames = data.get("frames") or []
    if not frames:
        return None
    best = min(frames, key=lambda f: (0 if float(f["t"]) <= t_s <= float(f["end"]) else 1, abs(float(f["t"]) - t_s)))
    path = os.path.join(store.frames_dir(row["content_hash"]), best["file"])
    return (path, float(best["t"])) if os.path.exists(path) else None


def _transcript_around(row: dict, t_s: float, *, radius_s: float = 45.0) -> str:
    data = store.read_json(store.stage_path(row["content_hash"], "transcript.json")) or {}
    segs = [s for s in (data.get("segments") or []) if float(s.get("end", 0)) >= t_s - radius_s and float(s.get("start", 0)) <= t_s + radius_s]
    if not segs:
        return ""
    return " ".join(f"[{_mmss(float(s['start']))}] {str(s.get('text') or '').strip()}" for s in segs)


async def _frame_image(path: str) -> str:
    from ..video.screen import _data_url

    return await asyncio.to_thread(_data_url, path, max_width=settings.video_caption_width)


async def build_question_prompt(
    message: str, videos: Sequence[dict], history: Sequence[dict]
) -> Tuple[List[dict], List[dict], List[Tuple[float, str]]]:
    """-> (messages for the model, evidence hits used, frames attached)."""
    from .vision import history_turns

    done = [v for v in videos if v.get("status") == "done"]
    names = {int(v["id"]): _display_name(v) for v in done}
    ids = [int(v["id"]) for v in done]
    hits = await video_index.retrieve(message, ids, top_k=settings.video_retrieve_top_k)

    times = mentioned_times(message)
    visual = is_visual(message)
    frames: List[Tuple[float, str]] = []
    around: List[str] = []
    # Timestamps the person named: the transcript around them, and the
    # frame on screen then when the question is visual (or names a time
    # and the video has frames — "what's at 14:22" is a look).
    for t in times[:3]:
        for v in done:
            near = _transcript_around(v, t)
            if near:
                around.append(f"({names[int(v['id'])]}, around {_mmss(t)}) {near}")
            if len(frames) < settings.video_answer_frames:
                fr = await _frame_exactly_at(v, t)
                if fr:
                    frames.append((fr[1], fr[0]))
    if visual and not times:
        # No time named: the frames of the best screen/visual hits.
        for h in hits:
            if h["modality"] in ("screen", "visual") and len(frames) < settings.video_answer_frames:
                for v in done:
                    if int(v["id"]) == int(h["analysis_id"]):
                        fr = _frame_at(v, float(h["start_s"]))
                        if fr and all(abs(fr[1] - f[0]) > 1.0 for f in frames):
                            frames.append((fr[1], fr[0]))
    lines: List[str] = ["VIDEOS:"]
    for v in done:
        u = Understanding.from_json(v.get("understanding") or {})
        lines.append(f"- {names[int(v['id'])]}: {_mmss(_duration_s(v))}, {u.content_type.replace('_', ' ')}" + (f", {v['language']}" if v.get("language") else ""))
        if u.summary:
            lines.append(f"  Summary: {u.summary.strip()}")
        if u.chapters:
            lines.append("  Chapters: " + "; ".join(f"[{_mmss(c.start_s)}] {c.title}" for c in u.chapters[:30]))
        if u.not_covered:
            lines.append(f"  Not covered: {u.not_covered.strip()}")
    pending = [v for v in videos if v.get("status") != "done"]
    for v in pending:
        lines.append(f"- {_display_name(v)}: analysis {v.get('status')}" + (f" ({v.get('error')})" if v.get("error") else ""))
    if around:
        lines += ["", "TRANSCRIPT AROUND THE TIMES MENTIONED:"] + around
    if hits:
        lines += ["", "RETRIEVED EVIDENCE (most relevant first):", video_index.format_hits(hits, names)]
    else:
        lines += ["", "RETRIEVED EVIDENCE: nothing in the index matched this question."]
    if frames:
        lines += ["", "ATTACHED FRAMES: " + ", ".join(f"frame at [{_mmss(t)}]" for t, _p in sorted(frames))]
    lines += ["", f"QUESTION: {message.strip()}"]
    content: List[dict] = [{"type": "text", "text": "\n".join(lines)}]
    for t, path in sorted(frames):
        try:
            content.append({"type": "image_url", "image_url": {"url": await _frame_image(path)}})
        except Exception:  # noqa: BLE001 — a frame that will not load is not an error
            log.warning("could not attach frame %s", path, exc_info=True)
    messages = [{"role": "system", "content": _SYSTEM}, *history_turns(history), {"role": "user", "content": content}]
    return messages, hits, frames


# ----------------------------------------------------------- artifacts --


def _slug(name: str) -> str:
    base = os.path.splitext(os.path.basename(name or "video"))[0].lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:40]
    return base or "video"


async def publish_artifacts(row: dict, *, user_id: Optional[int]) -> List[dict]:
    """Copy the analysis's files into the reports directory under a name
    that is unique per user, and describe them for meta.report_files."""
    if not settings.reports_dir or user_id is None:
        return []
    out: List[dict] = []
    src_dir = store.artifacts_dir(row["content_hash"])
    prefix = f"{_slug(_display_name(row))}-{row['content_hash'][:8]}-u{int(user_id)}"
    for a in row.get("artifacts") or []:
        name = str(a.get("filename") or "")
        src = os.path.join(src_dir, name)
        if not name or not os.path.isfile(src):
            continue
        stem, ext = os.path.splitext(name)
        dest_name = f"{prefix}.{stem}{ext}"
        dest = os.path.join(settings.reports_dir, dest_name)
        try:
            if not os.path.exists(dest):
                await asyncio.to_thread(shutil.copyfile, src, dest)
            size = os.path.getsize(dest)
        except OSError:
            log.warning("could not publish artifact %s", name, exc_info=True)
            continue
        out.append({"filename": dest_name, "type": ext.lstrip("."), "size": size})
    return out


def _video_meta(videos: Sequence[dict], hits: Sequence[dict], frames: Sequence[Tuple[float, str]]) -> dict:
    """Small on purpose: meta rides on every history load and share snapshot."""
    items = []
    for v in videos:
        u = Understanding.from_json(v.get("understanding") or {})
        items.append(
            {
                "analysis_id": int(v["id"]),
                "filename": _display_name(v),
                "upload_id": v.get("upload_id"),
                "status": v.get("status"),
                "error": v.get("error") or "",
                "duration_s": round(_duration_s(v), 1),
                "content_type": u.content_type,
                "language": v.get("language"),
                "has_audio": v.get("has_audio"),
                "counts": {k: v.get("counts", {}).get(k) for k in ("segments", "frames_kept", "ocr_frames", "captions", "chunks") if v.get("counts")},
                "chapters": [{"start": round(c.start_s, 1), "title": c.title} for c in u.chapters[:40]],
                "stages": {k: {"status": s.get("status"), "ms": s.get("ms")} for k, s in (v.get("stages") or {}).items()},
            }
        )
    return {
        "vision": "main+router",
        "videos": items,
        "evidence": [
            {"start": round(h["start_s"], 1), "end": round(h["end_s"], 1), "modality": h["modality"], "analysis_id": int(h["analysis_id"]), "text": h["text"][:200]}
            for h in hits[:12]
        ],
        "frames_shown": [round(t, 1) for t, _p in frames],
    }


# ------------------------------------------------------------ the engine --


async def run_video_engine(
    message: str,
    videos: Sequence[dict],
    history: Sequence[dict],
    emit: Emit,
    *,
    conversation_id: Optional[str],
    effort: str = "think",
    user_id: Optional[int] = None,
    attach_turn: bool = False,
) -> str:
    """Answer about the conversation's videos. Returns the answer text."""
    # Question turns are retrieval-grounded: the evidence is in the prompt and
    # the answer is mostly quotation with timestamps. Measured 2026-09-09 on
    # the live meeting video: with thinking on, 15-24 s to the first token
    # for a text question and 72 s with one frame attached; without it, the
    # same answers arrive in a few seconds. So Fast and Think both answer
    # without thinking; only Max keeps it. meta.effort reports what ran.
    picked = llm.normalize_effort(effort)
    level = "think" if picked == "max" else "fast"
    rows: List[dict] = []
    for row in videos:
        rows.append(await _wait_for_analysis(row, emit))

    failed = [r for r in rows if r.get("status") == "failed"]
    done = [r for r in rows if r.get("status") == "done"]
    parts: List[str] = []
    report_files: List[dict] = []
    for r in done:
        report_files.extend(await publish_artifacts(r, user_id=user_id))

    if failed and not done:
        lines = [f"I couldn't analyse **{_display_name(r)}**: {r.get('error') or 'the analysis failed'}." for r in failed]
        text = "\n\n".join(lines)
        await emit("token", {"text": text})
        await emit("meta", {"route": "video", "effort": picked, "video": _video_meta(rows, [], [])})
        return text

    if attach_turn and is_placeholder(message):
        # The understanding, straight from the analysis.
        for r in done:
            parts.append(overview_markdown(r))
        for r in failed:
            parts.append(f"I couldn't analyse **{_display_name(r)}**: {r.get('error') or 'the analysis failed'}.")
        text = "\n\n---\n\n".join(parts)
        # Stream it in pieces so the UI's caret moves; the content is fixed.
        for piece in _chunks(text, 400):
            await emit("token", {"text": piece})
        await emit(
            "meta",
            {"route": "video", "effort": picked, "report_files": report_files, "video": _video_meta(rows, [], [])},
        )
        return text

    messages, hits, frames = await build_question_prompt(message, rows, history)
    answer_parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        messages, model_choice="smart", effort=level, max_tokens=VISION_ANSWER_TOKENS
    ):
        if kind == "reasoning":
            await emit("reasoning", {"text": delta})
        else:
            answer_parts.append(delta)
            await emit("token", {"text": delta})
    answer = "".join(answer_parts)
    if failed:
        note = "\n\n" + "\n".join(f"_I couldn't analyse {_display_name(r)}: {r.get('error') or 'the analysis failed'}._" for r in failed)
        await emit("token", {"text": note})
        answer += note
    await emit(
        "meta",
        {"route": "video", "effort": level, "report_files": report_files, "video": _video_meta(rows, hits, frames)},
    )
    return answer


def _chunks(text: str, size: int) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def video_history(history: Sequence[dict]) -> List[dict]:
    """Text turns for the model (kept for symmetry with the other engines)."""
    return [m for m in recent_turns(history, settings.chat_history_turns) if isinstance(m.get("content"), str)]
