"""URL / website analysis engine (Phase 2).

When a user pastes URLs, fetch each through the SSRF-safe path, extract the
readable text, and store it on the conversation so follow-up questions reuse it
without re-fetching. Large pages are reduced to the parts relevant to the
question. Emits `status` progress and a `sources` meta (reusing the Phase 1
Sources panel).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Awaitable, Callable, List, Optional, Sequence
from urllib.parse import urlparse

from . import DIAGRAM_INSTRUCTION, recent_turns
from .. import db, llm
from ..config import settings
from ..core import extract, net
from ..core.urls import select_relevant

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

# Room per page in the prompt; the 131072 window comfortably holds several.
_PER_DOC_CHARS = 12000
_TOTAL_DOC_CHARS = 90000


async def _remember_globally(
    conversation_id: str, url: str, fetched: net.FetchResult, ext: extract.Extracted
) -> Optional[dict]:
    """A shared page joins the GLOBAL corpus, and its site joins the crawl queue.

    Until 2026-09-03 a pasted link was read into `url_documents` — this one
    conversation's memory — and nowhere else. The owner's point: a shared
    site "has multiple pages and more information"; one page in one chat is
    not knowledge. Now the page is stored where every search and every Fast
    answer reads from, and a bounded background crawl of the site is queued
    (engines/crawl.py) so the rest of it follows without anyone waiting.

    → {"host", "root_url", "job_id"} when a crawl was queued, else None.
    Never raises: the answer about the page must not depend on this.
    """
    if not settings.web_memory_enabled:
        return None
    from .search import _normalize_url, _provenance_of

    try:
        headers = getattr(fetched, "headers", None) or {}
        meta = _provenance_of(ext, fetched.url, fetched.content_type, headers)
        digest = hashlib.sha256((ext.text or "").encode("utf-8")).hexdigest()

        def _write() -> None:
            db.upsert_web_page(
                _normalize_url(url),
                url,
                fetched.url,
                ext.title or "",
                ext.text or "",
                fetched.content_type or "",
                200,
                digest,
                [],
                **meta,
            )

        await db.run_in_thread(_write)
    except Exception:  # noqa: BLE001 — memory is an enhancement
        log.debug("could not store shared page %s globally", url[:120], exc_info=True)
    if not settings.web_share_crawl_enabled:
        # Still worth waking the worker: the page above needs embedding.
        try:
            from .. import web_worker

            web_worker.kick()
        except Exception:  # noqa: BLE001
            pass
        return None
    try:
        from .crawl import enqueue_site_crawl

        job_id = await enqueue_site_crawl(conversation_id, url, kind="share")
    except Exception:  # noqa: BLE001
        job_id = None
    if job_id is None:
        return None
    host = (urlparse(url).hostname or url).removeprefix("www.")
    return {"host": host, "root_url": url, "job_id": job_id, "status": "queued"}


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
    await db.run_in_thread(
        db.save_url_document, conversation_id, url, ext.title, ext.text
    )
    site_crawl = await _remember_globally(conversation_id, url, fetched, ext)
    return {"url": url, "title": ext.title, "text": ext.text, "site_crawl": site_crawl}


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
    already = await db.run_in_thread(db.get_url_document_urls, conversation_id)
    queued_sites: List[dict] = []
    for url in urls:
        if url not in already:
            doc = await fetch_and_store(conversation_id, url, emit)
            if doc and doc.get("site_crawl"):
                queued_sites.append(doc["site_crawl"])
    if queued_sites:
        hosts = ", ".join(dict.fromkeys(s["host"] for s in queued_sites))
        await emit(
            "status",
            {"text": f"Indexing {hosts} in the background — later questions can draw on the whole site."},
        )

    docs = await db.run_in_thread(db.get_url_documents, conversation_id)
    if not docs:
        # Nothing could be fetched — a paywall, a login wall, a dead host, or a
        # page that is all JavaScript. That is a reason to say so and then still
        # ANSWER, not to stop. Replying with one sentence used to throw away
        # whatever else the message contained, which for a large paste was the
        # entire point of sending it (owner report, 2026-08-11).
        parts: List[str] = []
        messages = [
            {
                "role": "system",
                "content": (
                    "The links in the user's message could not be fetched — "
                    "they may need a login, block automated readers, or be "
                    "unreachable. Say that plainly in ONE short sentence, then "
                    "answer the rest of the message from what it already "
                    "contains and from your own knowledge. Never pretend you "
                    "read a page you did not. If the message carries pasted "
                    "text or a document, that content is the substance of the "
                    "request — use it."
                )
                + DIAGRAM_INSTRUCTION,
            },
            *recent_turns(history, 4),
            {"role": "user", "content": message},
        ]
        async for kind, delta in llm.stream_chat_events(messages, max_tokens=12000):
            await emit(kind, {"text": delta})
            if kind == "token":
                parts.append(delta)
        answer = "".join(parts).strip()
        if not answer:
            answer = (
                "I couldn't read any of those links, and I wasn't able to draft "
                "an answer from the rest of the message either. Paste the text "
                "you want me to work from and I'll take it from there."
            )
            await emit("token", {"text": answer})
        await emit("meta", {"route": "url", "fetch_failed": True})
        return answer

    parts: List[str] = []
    async for kind, delta in llm.stream_chat_events(
        _answer_messages(message, docs, history), max_tokens=12000
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)

    meta = {
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
    }
    if queued_sites:
        # Surfaced so the UI (and anyone reading the stored meta) can see
        # that the site is being indexed behind this answer.
        meta["site_crawl"] = queued_sites
    await emit("meta", meta)
    return "".join(parts)
