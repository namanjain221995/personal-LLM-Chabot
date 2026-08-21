"""Cross-chat memory (V9, 2026-07-23).

When a signed-in user asks something, we look through their OTHER conversations
for relevant material and hand the model a compact recall block — so it can
answer "what did we decide about X last time?" the way ChatGPT references past
chats. Retrieval is keyword-based over the user's own messages (db.py), scoped
to that user and excluding the current conversation.

Pure/offline: keyword extraction has no dependencies; the DB call is injected.
"""
from __future__ import annotations

import re
from typing import List, Optional

# Small stopword set so keywords are the content words of the question.
_STOPWORDS = {
    "the", "and", "for", "are", "was", "you", "your", "our", "what", "when",
    "where", "which", "who", "how", "why", "did", "does", "do", "can", "could",
    "would", "should", "about", "with", "that", "this", "these", "those",
    "from", "into", "have", "has", "had", "get", "got", "any", "all", "some",
    "tell", "show", "give", "know", "want", "please", "there", "here", "then",
    "than", "them", "they", "his", "her", "its", "not", "but", "also", "just",
    "like", "chat", "ask", "asked", "say", "said", "talk", "talked",
}
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_'-]{2,}")


def keywords(text: str, max_keywords: int = 8) -> List[str]:
    """Content words of a query: length ≥ 3, not a stopword, de-duplicated."""
    out: List[str] = []
    for raw in _WORD_RE.findall(text or ""):
        w = raw.lower()
        if w in _STOPWORDS or w in out:
            continue
        out.append(w)
        if len(out) >= max_keywords:
            break
    return out


def format_recall_block(hits: List[dict]) -> Optional[str]:
    """Render recall hits as a system-context block, or None when empty."""
    if not hits:
        return None
    lines = [
        "Context from the user's earlier conversations (reference it when it "
        "helps answer; otherwise ignore it, and don't mention it unless asked):",
    ]
    for h in hits:
        title = (h.get("title") or "Untitled").strip()
        snippet = (h.get("snippet") or "").strip()
        # Attribution matters: a fact the user stated is something to accept,
        # while one you asserted earlier is only as good as it ever was.
        speaker = {"user": " (the user said)", "assistant": " (you answered)"}.get(
            (h.get("role") or "").strip().lower(), ""
        )
        lines.append(f'- From "{title}"{speaker}: {snippet}')
    return "\n".join(lines)


def recall_block(
    user_id: int,
    query: str,
    exclude_conversation_id: Optional[str],
    *,
    search=None,
    limit: int = 3,
) -> Optional[str]:
    """Build the cross-chat recall block for `query`, or None when nothing is
    relevant. `search` defaults to db.recall_conversations (injectable for tests)."""
    kws = keywords(query)
    if not kws:
        return None
    if search is None:
        from . import db

        search = db.recall_conversations
    hits = search(user_id, kws, exclude_conversation_id, limit)
    return format_recall_block(hits)
