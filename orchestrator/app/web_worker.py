"""Keeps the public web corpus warm: drains the index, re-reads what went stale.

WHY IT LIVES IN THE ORCHESTRATOR. A separate container was considered and
rejected: the work is a few HTTP fetches every five minutes, the queue is a
PostgreSQL column (so a restart resumes rather than forgets), and the code it
needs — the SSRF-safe fetch, the extractor, the embedder client — is already
loaded in this process. A second image to run one asyncio loop would be a
build, a healthcheck and a deployment surface bought for nothing.

TWO JOBS, both idempotent and both interruptible:

  INDEX    embed pages that are stored but have no vectors. Indexing was
           write-behind with three call sites, all of them inside a search or
           a crawl — so a page whose embedding failed stayed invisible until
           somebody happened to run another search. Now something always
           comes back for it.

  REFRESH  re-read pages past their deadline, most-wanted first, using
           conditional requests so an unchanged page costs one 304 and no
           bandwidth. This is what stops the corpus quietly becoming a
           museum: a stale page that nobody re-reads is exactly how the
           platform would keep answering with last year's Vice President.

It never blocks a user request, and every failure is contained to one page.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from . import db, metrics, web_index
from .config import settings
from .freshness import Freshness

log = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None


def _ttl_for(row: Dict[str, Any]) -> int:
    """How long this page may go unread before it is worth re-fetching.

    Volatility is inferred from what the page IS, not from a hand-written URL
    list: an official site that names an office holder changes when the holder
    does, documentation changes on release, an encyclopaedia article about a
    historical event does not change at all. Demand shortens it — a page
    behind many answers is worth keeping sharp.
    """
    url = (row.get("url") or "").lower()
    title = (row.get("title") or "").lower()
    text = url + " " + title
    demand = int(row.get("retrieval_count") or 0)

    if any(k in text for k in ("price", "stock", "live", "score", "weather", "breaking")):
        ttl = settings.web_refresh_realtime_ttl_s
    elif any(
        k in text
        for k in (
            "president", "minister", "ceo", "governor", "chairman", "election",
            "current", "latest", "release", "version", "news", "appointed",
        )
    ):
        ttl = settings.web_refresh_recent_ttl_s
    else:
        ttl = settings.web_refresh_static_ttl_s

    if demand >= 10:
        ttl = max(3600, ttl // 3)
    elif demand >= 3:
        ttl = max(3600, ttl // 2)

    # A page that keeps failing backs off instead of being retried forever.
    failures = int(row.get("refresh_failures") or 0)
    if failures:
        ttl = min(ttl * (2 ** min(failures, 5)), 30 * 24 * 3600)
    return ttl


def _due_pages(limit: int) -> List[Dict[str, Any]]:
    """Pages past their deadline, most-retrieved first."""
    with db.connection() as con:
        return con.execute(
            """SELECT id, url, url_key, title, content_hash, etag, last_modified,
                      retrieval_count, refresh_failures, fetched_at
                 FROM web_pages
                WHERE next_refresh_at IS NOT NULL
                  AND next_refresh_at <= now()
                  AND text <> ''
                ORDER BY retrieval_count DESC, next_refresh_at
                LIMIT %s""",
            (limit,),
        ).fetchall()


def _schedule_next(page_id: int, ttl_seconds: int, *, failed: bool) -> None:
    """Stamp the next deadline, with jitter so a batch never re-converges."""
    jitter = random.uniform(0.85, 1.15)
    with db.connection() as con:
        con.execute(
            """UPDATE web_pages
                  SET next_refresh_at = now() + make_interval(secs => %s),
                      refresh_failures = CASE WHEN %s THEN refresh_failures + 1 ELSE 0 END
                WHERE id = %s""",
            (int(ttl_seconds * jitter), failed, page_id),
        )


async def _refresh_one(row: Dict[str, Any]) -> bool:
    """Re-read one page. True when it was read successfully.

    Delegates to search.refetch_page so there is exactly ONE fetch path in the
    codebase: same SSRF guards, same redirect re-validation, same size cap and
    timeout, same non-thread-safe-lxml executor, same content-hash rule (a
    changed hash resets indexed_at inside upsert_web_page, which is what puts
    the page back in the embedding queue automatically).
    """
    url = row.get("url") or ""
    if not url:
        return False
    try:
        from .engines.search import refetch_page

        result = await refetch_page(url, previous_hash=row.get("content_hash") or "")
    except Exception:  # noqa: BLE001 — one bad page must not stop the cycle
        log.debug("refresh failed for %s", url[:120], exc_info=True)
        return False
    if result is None:
        return False
    if result.get("changed"):
        # Only stamp last_changed_at when the CONTENT moved — a page re-read
        # daily would otherwise look freshly authored every day, and ranking
        # would trust it more than it deserves.
        await db.run_in_thread(_mark_changed, int(row["id"]))
    return True


def _mark_changed(page_id: int) -> None:
    with db.connection() as con:
        con.execute("UPDATE web_pages SET last_changed_at = now() WHERE id = %s", (page_id,))


async def run_once() -> Dict[str, int]:
    """One cycle: index the backlog, then refresh what is due."""
    done = {"indexed": 0, "refreshed": 0, "failed": 0}

    started = time.perf_counter()
    try:
        done["indexed"] = await web_index.index_pending(limit=40)
        metrics.worker_job("index", True, time.perf_counter() - started)
    except Exception:  # noqa: BLE001
        metrics.worker_job("index", False, time.perf_counter() - started)
        log.debug("index drain failed", exc_info=True)

    if not settings.web_knowledge_worker_enabled:
        return done

    started = time.perf_counter()
    try:
        rows = await db.run_in_thread(
            _due_pages, max(1, settings.web_refresh_max_pages_per_cycle)
        )
    except Exception:  # noqa: BLE001
        log.debug("could not read the refresh queue", exc_info=True)
        return done

    if rows:
        sem = asyncio.Semaphore(max(1, settings.web_refresh_concurrency))

        async def one(row: Dict[str, Any]) -> None:
            async with sem:
                ok = await _refresh_one(row)
                await db.run_in_thread(
                    _schedule_next, int(row["id"]), _ttl_for(row), failed=not ok
                )
                done["refreshed" if ok else "failed"] += 1

        await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
        metrics.worker_job("refresh", done["failed"] == 0, time.perf_counter() - started)

        # Newly-changed pages had their watermark cleared; embed them now
        # rather than waiting a whole cycle to become answerable.
        try:
            done["indexed"] += await web_index.index_pending(limit=40)
        except Exception:  # noqa: BLE001
            pass
    return done


async def _loop() -> None:
    # A short delay before the first cycle: startup already has a migration,
    # a model handshake and the first requests competing for the box.
    await asyncio.sleep(45)
    while True:
        try:
            result = await run_once()
            if any(result.values()):
                log.info(
                    "web knowledge worker: indexed=%(indexed)d refreshed=%(refreshed)d "
                    "failed=%(failed)d",
                    result,
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any one cycle
            log.warning("web knowledge worker cycle failed", exc_info=True)
        await asyncio.sleep(max(30, settings.web_worker_interval_s))


def start() -> None:
    """Called from the FastAPI lifespan. Idempotent."""
    global _task
    if not settings.web_knowledge_worker_enabled:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="web-knowledge-worker")
    log.info(
        "web knowledge worker started (every %ss, %s pages/cycle)",
        settings.web_worker_interval_s,
        settings.web_refresh_max_pages_per_cycle,
    )


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    _task = None
