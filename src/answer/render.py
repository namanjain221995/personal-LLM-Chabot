"""Structured answer -> the text a user reads.

Deterministic, so the same verified answer always reads the same way and the
JSON never reaches a user who asked a business question.
"""
from __future__ import annotations

import re

from .models import GroundedAnswer


def _key(text: str) -> str:
    """Two sentences that differ only in case, spacing or end punctuation are one."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(" .!")


def dedupe(answer: GroundedAnswer) -> GroundedAnswer:
    """Drop a sentence the model wrote twice.

    The model sometimes puts the same line in `notes` and in `freshness_note`
    (seen live: the data-age sentence printed twice). The contract gives each
    fact one field; a note that repeats the summary, the freshness note, or an
    earlier note is removed. Only exact repeats -- a different sentence about
    the same thing is left alone, because judging meaning is not this layer's
    job.
    """
    seen = {_key(answer.summary)}
    if answer.freshness_note:
        seen.add(_key(answer.freshness_note))
    notes: list[str] = []
    for note in answer.notes:
        key = _key(note)
        if key and key not in seen:
            seen.add(key)
            notes.append(note)
    answer.notes = notes
    return answer


def render(answer: GroundedAnswer) -> str:
    lines: list[str] = []
    summary = answer.summary.strip()

    if answer.details:
        # A summary that already ends in a colon is introducing the list.
        lines.append(summary if summary.endswith(":") else f"{summary}")
        lines.append("")
        numbered = len(answer.details) > 1
        for index, detail in enumerate(answer.details, start=1):
            prefix = f"{index}. " if numbered else "- "
            lines.append(f"{prefix}{detail.text.strip()}")
    elif summary:
        lines.append(summary)

    for note in answer.notes:
        lines.append("")
        lines.append(note.strip())

    if answer.freshness_note:
        lines.append("")
        lines.append(answer.freshness_note.strip())

    return "\n".join(lines).strip()
