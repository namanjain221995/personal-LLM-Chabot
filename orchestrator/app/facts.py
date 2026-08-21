"""Explicit fact store (V10, 2026-08-21) — ChatGPT-style Memory.

When the user states something durable ("Sahil Patel is the CEO of TechSara",
"my name is Naman", "always answer in Hindi"), a background call to the small
router model extracts it as a short third-person fact and stores it in the
`user_facts` table. Every later assistant-mode request injects the user's
facts as a labelled system block, so the model "remembers" without any
fine-tuning — memory is retrieval, exactly as ChatGPT does it.

Extraction runs CONCURRENTLY with answer generation (it reads only the user's
message, not the answer), so it adds zero latency; its result rides out on
the final meta as `memory_updated` when it lands before the answer finishes.
Everything degrades to "no memory update" on failure — never a failed chat.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)

FACTS_HEADER = (
    "Durable facts this user has told you in past conversations (their saved "
    "memory — treat as true for this user unless they correct you; don't "
    "mention this list unless asked):"
)

# Bounds keep the block and the extractor prompt from growing without limit.
_FACT_MAX_CHARS = 300
_BLOCK_MAX_CHARS = 6000
_MESSAGE_MAX_CHARS = 4000
_MESSAGE_MIN_CHARS = 8

_EXTRACT_SYSTEM = """You maintain a user's long-term memory for a chat assistant.

Given the user's newest message and their currently saved facts, decide what
to remember. A fact is a short, durable, third-person statement about the
user or the world as the user declares it ("Sahil Patel is the CEO of
TechSara", "The user's name is Naman", "The user prefers answers in Hindi").

Do NOT store: questions, requests, greetings, opinions about the current
task, anything transient ("today", "this file"), or anything already saved.
If a new statement contradicts or updates a saved fact, replace that fact.

Reply with ONLY a JSON object, no other text:
{"add": ["<new fact>", ...], "replace": [{"id": <saved fact id>, "fact": "<rewritten fact>"}, ...]}
Use {"add": [], "replace": []} when there is nothing durable to remember."""


def facts_block(facts: List[dict]) -> Optional[str]:
    """Render saved facts as the system block, or None when there are none."""
    if not facts:
        return None
    lines = [FACTS_HEADER]
    used = len(FACTS_HEADER)
    for f in facts:
        line = f"- {f['fact']}"
        if used + len(line) > _BLOCK_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else None


def parse_extraction(raw: str) -> dict:
    """The extractor's JSON, tolerantly parsed. {} when unusable."""
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    raw_add = data.get("add")
    add = [
        _flatten(f)
        for f in (raw_add if isinstance(raw_add, list) else [])
        if isinstance(f, str) and f.strip()
    ]
    raw_replace = data.get("replace")
    replace = []
    for item in raw_replace if isinstance(raw_replace, list) else []:
        if not isinstance(item, dict):
            continue
        fact = item.get("fact")
        try:
            fact_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if isinstance(fact, str) and fact.strip():
            replace.append({"id": fact_id, "fact": _flatten(fact)})
    return {"add": add, "replace": replace}


def _flatten(text: str) -> str:
    """One whitespace-normalized line, capped. A fact with embedded newlines
    would escape its bullet in facts_block and read as fresh top-level system
    lines — a durable prompt-injection channel for text the extractor was fed.
    Same normalization memory_api applies to manual adds."""
    return " ".join((text or "").split())[:_FACT_MAX_CHARS]


def _normalized(text: str) -> str:
    return " ".join((text or "").lower().split()).rstrip(".")


async def remember_from_message(
    user_id: int,
    user_text: str,
    conversation_id: Optional[str],
    *,
    complete=None,
) -> List[dict]:
    """Extract and store durable facts from one user message.

    Returns the facts that were added or rewritten (empty when none).
    `complete` defaults to llm.router_chat_completion — injectable for tests.
    """
    if not settings.fact_extraction_enabled:
        return []
    text = (user_text or "").strip()
    if len(text) < _MESSAGE_MIN_CHARS:
        return []
    try:
        existing = await db.run_in_thread(
            db.list_user_facts, user_id, settings.memory_max_facts
        )
        saved_lines = "\n".join(
            f"[{f['id']}] {f['fact']}" for f in existing
        ) or "(none yet)"
        if complete is None:
            complete = llm.router_chat_completion
        raw = await complete(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Saved facts:\n{saved_lines}\n\n"
                        f"User's newest message:\n{text[:_MESSAGE_MAX_CHARS]}"
                    ),
                },
            ],
            max_tokens=400,
        )
        ops = parse_extraction(raw)
        if not ops:
            return []
        known = {_normalized(f["fact"]): f["id"] for f in existing}
        stored: List[dict] = []
        added = 0
        for item in ops.get("replace", []):
            updated = await db.run_in_thread(
                db.update_user_fact, user_id, item["id"], item["fact"]
            )
            if updated:
                known[_normalized(item["fact"])] = updated["id"]
                stored.append(updated)
        for fact in ops.get("add", []):
            if _normalized(fact) in known:  # extractor re-suggested a saved fact
                continue
            # Replaces rewrite existing rows; only genuine adds consume slots.
            if len(existing) + added >= settings.memory_max_facts:
                break
            created = await db.run_in_thread(
                db.add_user_fact, user_id, fact, conversation_id
            )
            known[_normalized(fact)] = created["id"]
            stored.append(created)
            added += 1
        return stored
    except Exception:
        log.warning("fact extraction failed", exc_info=True)
        return []
