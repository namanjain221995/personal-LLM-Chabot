"""URL / website analysis engine (Phase 2).

When a user pastes URLs, fetch each through the SSRF-safe path, extract the
readable text, and store it on the conversation so follow-up questions reuse it
without re-fetching. Large pages are reduced to the parts relevant to the
question. Emits `status` progress and a `sources` meta (reusing the Phase 1
Sources panel).
"""
from __future__ import annotations

from typing import Awaitable, Callable, List, Optional, Sequence
from urllib.parse import urlparse

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import db, llm
from ..config import settings
from ..core import extract, net
from ..core.urls import select_relevant

Emit = Callable[[str, dict], Awaitable[None]]

# Room per page in the prompt; the 131072 window comfortably holds several.
_PER_DOC_CHARS = 12000
_TOTAL_DOC_CHARS = 90000


async def fetch_and_store(
    conversation_id: str, url: str, emit: Emit
) -> Optional[dict]:
    """Fetch+extract one URL and store it; returns the doc or None on failure."""
    domain = urlparse(url).hostname or url
    await emit("status", {"text": f"Reading {domain}…"})
    try:
        fetched = await net.safe_fetch(
            url,
            timeout_ms=settings.fetch_timeout_ms,
            max_bytes=settings.fetch_max_bytes,
            accept="text/html,application/pdf,text/plain",
        )
    except net.UnsafeURLError:
        await emit("status", {"text": f"Skipped {domain} (blocked address)."})
        return None
    except net.FetchError:
        await emit("status", {"text": f"Couldn't reach {domain}."})
        return None
    try:
        ext = extract.extract_readable(fetched.content_type, fetched.body, fetched.url)
    except extract.UnsupportedContentError:
        await emit("status", {"text": f"{domain} isn't a readable page."})
        return None
    if not ext.text.strip():
        return None
    db.save_url_document(conversation_id, url, ext.title, ext.text)
    return {"url": url, "title": ext.title, "text": ext.text}


def _context_block(docs: List[dict], question: str) -> str:
    parts: List[str] = []
    budget = _TOTAL_DOC_CHARS
    for i, d in enumerate(docs, start=1):
        share = min(_PER_DOC_CHARS, max(1000, budget // max(1, len(docs))))
        body = select_relevant(d["text"], question, share)
        parts.append(f"[{i}] {d['title']} ({d['url']})\n{body}")
    return "\n\n".join(parts)


def _answer_messages(
    question: str, docs: List[dict], history: Sequence[dict]
) -> List[dict]:
    system = (
        "You answer questions about the web pages provided below. Use only "
        "their content; cite the page you rely on with a bracketed number like "
        "[1]. If the pages don't contain the answer, say so."
    )
    user = f"Pages:\n{_context_block(docs, question)}\n\nQuestion: {question}"
    return [{"role": "system", "content": system + DIAGRAM_INSTRUCTION}, *recent_turns(history, 4),
            {"role": "user", "content": user}]


async def run_url_engine(
    message: str,
    urls: List[str],
    conversation_id: str,
    history: Sequence[dict],
    emit: Emit,
) -> str:
    """Fetch any new URLs, then answer from all pages stored for this chat."""
    already = db.get_url_document_urls(conversation_id)
    for url in urls:
        if url not in already:
            await fetch_and_store(conversation_id, url, emit)

    docs = db.get_url_documents(conversation_id)
    if not docs:
        note = "I couldn't read any of those links."
        await emit("token", {"text": note})
        await emit("meta", {"route": "url"})
        return note

    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _answer_messages(message, docs, history), max_tokens=12000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)

    await emit(
        "meta",
        {
            "route": "url",
            "sources": [
                {
                    "n": i,
                    "title": d["title"],
                    "url": d["url"],
                    "domain": urlparse(d["url"]).hostname or d["url"],
                }
                for i, d in enumerate(docs, start=1)
            ],
        },
    )
    return "".join(parts)
