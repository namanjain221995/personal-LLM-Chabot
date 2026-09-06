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
from .. import db, llm, metrics
from ..config import settings
from ..core import extract, net, robots
from ..core.urls import check_shareable, select_relevant

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

# Room per page in the prompt; the 131072 window comfortably holds several.
_PER_DOC_CHARS = 12000
_TOTAL_DOC_CHARS = 90000


#: What the sharer is told when their link stays private. Keyed by the
#: refusal class (core/urls.SHARE_REFUSAL_REASONS); classes with no entry get
#: the bare sentence. Never the URL, never the parameter — the sharer knows
#: what they pasted, and a progress line is stored with the conversation.
_REFUSAL_NOTE = {
    "credential_query": "it carries a credential",
    "userinfo": "it carries a credential",
    "ip_literal": "it points at a private address",
    "internal_host": "it points at a private address",
    "port": "it points at a private address",
}


def _wake_worker() -> None:
    """The knowledge worker embeds new pages on its own clock; a kick makes a
    just-stored page searchable sooner. Best effort, never the answer."""
    try:
        from .. import web_worker

        web_worker.kick()
    except Exception:  # noqa: BLE001
        pass


async def _remember_globally(
    conversation_id: str,
    url: str,
    fetched: net.FetchResult,
    ext: extract.Extracted,
    user_id: Optional[int] = None,
    emit: Optional[Emit] = None,
) -> Optional[dict]:
    """A shared page joins the GLOBAL corpus, and its site joins the crawl queue.

    Until 2026-09-03 a pasted link was read into `url_documents` — this one
    conversation's memory — and nowhere else. The owner's point: a shared
    site "has multiple pages and more information"; one page in one chat is
    not knowledge. Now the page is stored where every search and every Fast
    answer reads from, and a bounded background crawl of the site is queued
    (engines/crawl.py) so the rest of it follows without anyone waiting.

    THE BOUNDARY (security review, 2026-09-03). A pasted link is the sharer's
    to read; it is not automatically the workspace's to keep. A pre-signed S3
    or Azure SAS URL, a `user:pass@` URL, an internal host or port, an OAuth
    callback: there the URL IS the credential, and the body we fetched
    through it is private to whoever held it. Writing that body to web_pages
    would hand it to every member's next Fast answer; queueing a crawl would
    go looking for more of it. `core/urls.check_shareable` decides from the
    URL alone — the pasted one AND the one the fetch landed on, because a
    short link that 302s to a signed object is the same leak with one hop —
    and a refused page is never written to web_pages and never crawled. It
    is still in url_documents (fetch_and_store wrote it before calling
    here), so the sharer's own answer is exactly what it was. The refusal
    class is logged and counted; the URL is not, since the log is precisely
    where a signature must not end up.

    → {"host", "root_url", "job_id", "status"} whenever a site crawl was
      ATTEMPTED: status "queued" with the new job's id, or "existing" with
      job_id None when the queue already held (or recently finished) this
      scope. The caller announces both the same way — see run_url_engine for
      why telling them apart to the user is a cross-user oracle. None when
      the page stayed private, the corpus is off, or the crawl is off.
    Never raises: the answer about the page must not depend on this.
    """
    if not settings.web_memory_enabled:
        return None

    pasted = check_shareable(url)
    landed = pasted if not fetched.url or fetched.url == url else check_shareable(fetched.url)
    for where, decision in (("pasted", pasted), ("final", landed)):
        if decision.url is not None:
            continue
        log.info(
            "shared link kept private to its conversation: reason=%s where=%s",
            decision.reason, where,
        )
        metrics.inc(
            "knowledge_share_refused_total",
            "Pasted links kept out of the shared web corpus, by refusal class",
            reason=decision.reason,
            where=where,
        )
        if emit is not None:
            note = _REFUSAL_NOTE.get(decision.reason)
            text = "Kept this link private to this conversation"
            await emit("status", {"text": text + (f" — {note}." if note else ".")})
        return None
    shared_url, canonical_url = pasted.url, landed.url
    if pasted.stripped:
        log.info(
            "shared link stored without %d tracking parameter(s): %s",
            len(pasted.stripped), ",".join(pasted.stripped),
        )

    from .search import _normalize_url, _provenance_of

    try:
        headers = getattr(fetched, "headers", None) or {}
        meta = _provenance_of(ext, canonical_url, fetched.content_type, headers)
        digest = hashlib.sha256((ext.text or "").encode("utf-8")).hexdigest()

        def _write() -> None:
            db.upsert_web_page(
                _normalize_url(shared_url),
                shared_url,
                canonical_url,
                ext.title or "",
                ext.text or "",
                fetched.content_type or "",
                200,
                digest,
                [],
                # V16 provenance: who brought the page in, and how. 'share'
                # is the lowest-trust origin — the page is cited but never
                # retires other evidence — until an independent search finds
                # it. The introducer makes the row attributable and, should
                # the member be removed, purgeable. user_id is None until
                # main.py threads it (see run_url_engine); the conversation
                # still names the owner via conversation_owner.
                origin="share",
                # V22: which extractor produced this text (see crawl/search).
                extract_version=extract.EXTRACT_VERSION,
                introduced_by_user_id=user_id,
                introduced_in_conversation_id=conversation_id,
                **meta,
            )

        await db.run_in_thread(_write)
    except Exception:  # noqa: BLE001 — memory is an enhancement
        log.debug("could not store shared page %s globally", shared_url[:120], exc_info=True)
    if not settings.web_share_crawl_enabled or not settings.web_background_crawl_enabled:
        # Nothing to announce. enqueue_site_crawl would also return None with
        # the crawl feature off, but that None is indistinguishable from
        # "already queued" — and the caller must tell those apart, because
        # only one of them is a true "indexing" statement. Still worth waking
        # the worker: the page above needs embedding.
        _wake_worker()
        return None
    try:
        from .crawl import enqueue_site_crawl

        job_id = await enqueue_site_crawl(conversation_id, shared_url, kind="share")
    except Exception:  # noqa: BLE001
        job_id = None
    if job_id is None:
        # Deduped by scope (queued, running, or crawled within 24 h) or a
        # queue failure. The site's pages are, or are about to be, in the
        # corpus either way; only the embedding kick is still ours to do.
        _wake_worker()
    host = (urlparse(shared_url).hostname or shared_url).removeprefix("www.")
    return {
        "host": host,
        "root_url": shared_url,
        "job_id": job_id,
        "status": "queued" if job_id is not None else "existing",
    }


async def fetch_and_store(
    conversation_id: str, url: str, emit: Emit, user_id: Optional[int] = None
) -> Optional[dict]:
    """Fetch+extract one URL and store it; returns the doc or None on failure."""
    domain = urlparse(url).hostname or url
    await emit("status", {"text": f"Reading {domain}…"})
    # robots.txt on the pasted-link read too (finding K6). This path is
    # user-directed rather than a crawl, but it is still this server fetching a
    # third-party page automatically, and the site's stated rules are the only
    # signal it has to say no. Fail-open: `robots.allowed` allows when
    # robots.txt could not be READ at all, so a site outage never turns into
    # "this platform cannot open links any more".
    try:
        if not await robots.allowed(url):
            await emit(
                "status",
                {"text": f"Skipped {domain} (its robots.txt asks bots not to read that page)."},
            )
            return None
        if not await robots.reserve_slot(url):
            await emit(
                "status",
                {"text": f"Skipped {domain} (it asks for a longer delay between requests)."},
            )
            return None
    except Exception:  # noqa: BLE001 — the robots check is a courtesy, not a gate
        log.debug("robots check unavailable for %s", domain, exc_info=True)
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
    # The sharer's own memory of the page, always — whatever the shared
    # corpus decides below. Written first so a refused link still answers.
    await db.run_in_thread(
        db.save_url_document, conversation_id, url, ext.title, ext.text
    )
    site_crawl = await _remember_globally(
        conversation_id, url, fetched, ext, user_id=user_id, emit=emit
    )
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
    effort: str = "think",
    user_id: Optional[int] = None,
) -> str:
    """Fetch any new URLs, then answer from all pages stored for this chat.

    `effort` is the composer's Fast/Think/Max (2026-09-03). Until then this
    engine called stream_chat_events without it — the default is thinking
    ON — so a shared link at Fast measured 19 s to the first token, 18 of
    them reasoning. The fourth engine to make the same omission; the rule
    stands: never call stream_chat_events on a user-facing route without it.

    `user_id` (2026-09-03, V16) is who is sharing; it is recorded on the
    shared page as its introducer. main.py does not pass it yet — the
    neighbouring search, agent and deep-research calls already do, as
    `user_id=int(signed_in["id"]) if signed_in is not None else None`.
    TODO(main.py, run_url_engine call): pass the same. Until then the
    introducer column stays NULL and the conversation id alone attributes the
    row (conversation_owner resolves it to the member).
    """
    level = llm.normalize_effort(effort)
    already = await db.run_in_thread(db.get_url_document_urls, conversation_id)
    attempted: List[dict] = []
    for url in urls:
        if url not in already:
            doc = await fetch_and_store(conversation_id, url, emit, user_id=user_id)
            if doc and doc.get("site_crawl"):
                attempted.append(doc["site_crawl"])
    if attempted:
        # THE ORACLE (security review, 2026-09-03). This line used to appear
        # only when a NEW crawl job was created, and the queue dedups by site
        # across every conversation and every member for 24 h. So its
        # presence or absence told a member whether someone else in the
        # workspace had shared this site recently — a cross-user probe that
        # needs nothing but a URL. Now the line is the same whether this
        # request created the job or the queue already held the scope: both
        # are true ("the site is being, or has been, indexed"), and only
        # meta.site_crawl below — a fact about THIS request's own job —
        # distinguishes them.
        hosts = ", ".join(dict.fromkeys(s["host"] for s in attempted))
        await emit(
            "status",
            {"text": f"Indexing {hosts} in the background — later questions can draw on the whole site."},
        )
    queued_sites = [s for s in attempted if s.get("job_id") is not None]

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
        async for kind, delta in llm.stream_chat_events(
            messages, effort=level, max_tokens=12000
        ):
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
        _answer_messages(message, docs, history), effort=level, max_tokens=12000
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
        # that the site is being indexed behind this answer. Only jobs THIS
        # request created: a job that already existed belongs to whoever
        # shared the site first, and is not this member's to see.
        meta["site_crawl"] = queued_sites
    await emit("meta", meta)
    return "".join(parts)
