"""Rolling conversation summary (Phase A).

The summary is what lets a conversation continue after older turns leave the
context window. It is rebuilt INCREMENTALLY — previous summary + only the
newly folded turns — so the cost of one compaction is constant no matter how
long the conversation has grown.

Nothing here performs network I/O at import time.
"""
from __future__ import annotations

from typing import List, Sequence

from . import llm
from .config import settings

_SYSTEM = (
    "You maintain a running summary of one conversation so it can continue "
    "after older messages are removed."
)

_INSTRUCTIONS = """Rewrite the EXISTING SUMMARY so it also covers the NEW MESSAGES.

Keep, verbatim where possible: names, identifiers, numbers, dates, file and
table names, decisions made, constraints the user stated, open questions, and
anything the user asked you to remember.
Drop: greetings, acknowledgements, restated questions, and your own
explanations of things already resolved.
Never invent. If something is uncertain, mark it "(unconfirmed)".
Write compact factual notes under headings, not prose. Max ~1500 words.
Output ONLY the rewritten summary — no preamble, no commentary."""

# A folded turn is clipped before it reaches the summarizer: one enormous
# pasted document must not blow the summarization call's own window.
_MAX_TURN_CHARS = 4000


def format_turns(turns: Sequence[dict]) -> str:
    """Role-tagged transcript of the turns being folded."""
    lines: List[str] = []
    for t in turns:
        content = t.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = t.get("role", "user")
        text = content.strip()
        if len(text) > _MAX_TURN_CHARS:
            text = text[:_MAX_TURN_CHARS] + " …[truncated]"
        lines.append(f"[{role}] {text}")
    return "\n\n".join(lines)


def build_messages(existing: str, turns: Sequence[dict]) -> List[dict]:
    """The summarization prompt: previous summary + only the new turns."""
    user = (
        f"{_INSTRUCTIONS}\n\n"
        f"EXISTING SUMMARY:\n{existing.strip() or '(none yet)'}\n\n"
        f"NEW MESSAGES:\n{format_turns(turns)}"
    )
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user},
    ]


async def summarize(existing: str, turns: Sequence[dict]) -> str:
    """Fold `turns` into `existing`, returning the new summary text.

    Raises on model failure; the caller decides what to do (compaction is
    best-effort — a chat must never break because summarizing failed).
    """
    if not format_turns(turns).strip():
        return existing
    text = await llm.chat_completion(
        build_messages(existing, turns),
        temperature=0.0,
        max_tokens=settings.summary_max_tokens,
    )
    return (text or "").strip() or existing


async def condense(summary: str) -> str:
    """Re-summarize the summary alone when it approaches its own cap.

    Without this the summary grows forever and eventually crowds out the very
    turns it exists to make room for.
    """
    text = await llm.chat_completion(
        [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    "Condense the following conversation summary to about half "
                    "its length. Keep every name, identifier, number, date, "
                    "decision, constraint and open question. Drop redundancy "
                    "and anything already superseded. Output ONLY the "
                    f"condensed summary.\n\n{summary}"
                ),
            },
        ],
        temperature=0.0,
        max_tokens=settings.summary_max_tokens,
    )
    return (text or "").strip() or summary


SUMMARY_HEADER = (
    "Summary of earlier messages in THIS conversation (older turns were "
    "compacted to save space). Treat these as established facts:"
)


def summary_block(summary: str) -> dict:
    """The system message carrying the summary into a prompt."""
    return {"role": "system", "content": f"{SUMMARY_HEADER}\n{summary.strip()}"}
