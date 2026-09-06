"""Keeps the public web corpus warm: drains the index, re-reads what went stale.

WHY IT LIVES IN THE ORCHESTRATOR. A separate container was considered and
rejected: the work is a few HTTP fetches every five minutes, the queue is a
PostgreSQL column (so a restart resumes rather than forgets), and the code it
needs — the SSRF-safe fetch, the extractor, the embedder client — is already
loaded in this process. A second image to run one asyncio loop would be a
build, a healthcheck and a deployment surface bought for nothing.

TWO JOBS, both idempotent and both interruptible:

  INDEX    embed pages that are stored but have no vectors, and RE-CHUNK pages
           whose vectors an older chunker produced (V24). Indexing was
           write-behind with three call sites, all of them inside a search or
           a crawl — so a page whose embedding failed stayed invisible until
           somebody happened to run another search. Now something always
           comes back for it.

           The re-chunk half needs NO NETWORK. `web_pages.text` is retained, so
           a chunker change is repaired from the store: no fetch, no ETag, no
           robots check, nothing a remote server could refuse. That is the
           whole reason it is a separate column from `extract_version` — a page
           can be provably current upstream (a 304) and still be overdue for
           reprocessing here.

  REFRESH  re-read pages past their deadline, most-wanted first, using
           conditional requests so an unchanged page costs one 304 and no
           bandwidth. This is what stops the corpus quietly becoming a
           museum: a stale page that nobody re-reads is exactly how the
           platform would keep answering with last year's Vice President.

           That paragraph described an intention, not the code, until
           2026-09-06 (finding K5): `etag` and `last_modified` were stored
           and never read, `_refresh_one` returned a boolean that had no way
           to say "304", and every refresh was a full download. Both halves
           are now real, and `run_once` counts `not_modified` separately so
           the difference is visible rather than assumed. robots.txt is
           consulted on this path too (K6) — the refresh worker is the one
           fetcher on this box with nobody waiting on it, so it obeys a
           Disallow and waits out a Crawl-delay in full.

           A page also goes stale when the EXTRACTOR improves (V21/V22): the
           URL still says the same thing, but our stored copy of it is worse
           than what we could read today, and the vector chunks were built
           from that worse copy. Those rows join the same queue, drained in
           the same demand order inside the same budget — the alternative
           was a mass recrawl of the whole corpus.

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
from .core.extract import EXTRACT_VERSION
from .freshness import Freshness

log = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
#: Set by `kick()` when something is worth doing NOW — a crawl was just
#: queued because a user shared a link. The loop otherwise sleeps its full
#: interval, and a shared site that took five minutes to even start indexing
#: would not feel like "in the background", it would feel broken.
_wake: Optional[asyncio.Event] = None


def kick() -> None:
    """Wake the loop early. Safe from any coroutine; a no-op when not running."""
    if _wake is not None:
        try:
            _wake.set()
        except RuntimeError:  # pragma: no cover — loop closing
            pass


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


_DUE_COLUMNS = (
    "id, url, url_key, title, content_hash, etag, last_modified, "
    "retrieval_count, refresh_failures, fetched_at"
)

#: The queue as it was before V21: deadline only.
_DUE_SQL = (
    f"SELECT {_DUE_COLUMNS} FROM web_pages "
    "WHERE next_refresh_at IS NOT NULL AND next_refresh_at <= now() "
    "AND text <> '' "
    "ORDER BY retrieval_count DESC, next_refresh_at LIMIT %s"
)

#: V21 (recovered 2026-09-06): the SAME bounded queue, widened by one term.
#:
#: A better extractor makes every previously-stored page stale in a way no
#: deadline knows about — the URL has not changed, so nothing would ever
#: re-read it, and its LanceDB chunks stay built from the worse text. Rather
#: than a mass re-crawl, rows below the current extractor version join the
#: ordinary refresh queue and are drained most-retrieved-first inside the
#: existing per-cycle budget. There is no new scan and no new loop: one extra
#: OR term, the same LIMIT, the same ordering.
#:
#: TWO GUARDS KEEP THE NEW TERM BOUNDED, and both are load-bearing at a
#: budget of 8 pages per 5-minute cycle ordered by demand — without them the
#: SAME eight rows would be re-read every cycle forever.
#:
#: `refresh_failures = 0` covers failure. A stale-extractor page that fails
#: leaves this term at once (`_schedule_next` bumps the counter) and comes
#: back only through the deadline term, which already backs off
#: exponentially.
#:
#: `fetched_at` spacing covers success. The term is supposed to end when the
#: page is re-read and its new `extract_version` is written — but that write
#: happens in `upsert_web_page`'s callers, and a caller that forgets to pass
#: the version would leave the row at its old number and re-offer it on the
#: very next cycle. One un-deadlined attempt per page per window turns that
#: class of bug into a few wasted fetches instead of a starved queue. Six
#: hours is comfortably shorter than a full pass over the corpus (2,208 rows
#: at 8 per 300 s ≈ 23 h), so it costs the intended migration nothing.
_EXTRACT_UPGRADE_MIN_SPACING_S = 6 * 3600

_DUE_SQL_V21 = (
    f"SELECT {_DUE_COLUMNS}, extract_version FROM web_pages "
    "WHERE text <> '' AND ("
    " (next_refresh_at IS NOT NULL AND next_refresh_at <= now())"
    " OR (extract_version < %s AND refresh_failures = 0"
    "     AND fetched_at < now() - make_interval(secs => %s))"
    ") "
    "ORDER BY retrieval_count DESC, next_refresh_at NULLS LAST LIMIT %s"
)

#: Cleared the first time PostgreSQL says the column is not there — i.e. on a
#: deployment where the V21 migration has not run yet. The refresh queue must
#: keep working in that window; it simply cannot see stale-extractor rows.
_HAS_EXTRACT_VERSION = True


def _due_pages(limit: int) -> List[Dict[str, Any]]:
    """Pages past their deadline, or stored by an older extractor.

    Most-retrieved first in both cases: the pages behind the most answers are
    the ones worth spending the cycle's budget on.
    """
    global _HAS_EXTRACT_VERSION
    if _HAS_EXTRACT_VERSION:
        try:
            with db.connection() as con:
                return con.execute(
                    _DUE_SQL_V21,
                    (int(EXTRACT_VERSION), _EXTRACT_UPGRADE_MIN_SPACING_S, limit),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            if not _missing_extract_version(exc):
                raise
            _HAS_EXTRACT_VERSION = False
            log.warning(
                "web_pages.extract_version is absent (V21 migration not applied "
                "yet); refreshing on deadline only until it is"
            )
    with db.connection() as con:
        return con.execute(_DUE_SQL, (limit,)).fetchall()


def _missing_extract_version(exc: BaseException) -> bool:
    """True when `exc` is PostgreSQL complaining about the V21 column only.

    Narrow on purpose: any other database error must still surface, or a real
    fault would be silently downgraded to "run the old query".
    """
    try:
        import psycopg

        if isinstance(exc, psycopg.errors.UndefinedColumn):
            return "extract_version" in str(exc)
    except Exception:  # noqa: BLE001 — psycopg shape changed; fall through
        pass
    return False


def _stale_extractor(row: Dict[str, Any]) -> bool:
    """True when this row is queued because OUR COPY is behind, not the site.

    `extract_version` is only in the row when `_DUE_SQL_V21` ran, i.e. when the
    column exists. A missing key, a NULL, and a version at or above the current
    one all mean the same thing here: ordinary freshness, keep the conditional
    request. Only a row we can see is behind earns an unconditional download.
    """
    version = row.get("extract_version")
    if version is None:  # absent key, or a NULL: unknown, not "behind"
        return False
    try:
        return int(version) < int(EXTRACT_VERSION)
    except (TypeError, ValueError):
        return False


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


async def _refresh_one(row: Dict[str, Any]) -> str:
    """Re-read one page. Returns the outcome, which is NOT just pass/fail.

    Delegates to search.refetch_page so there is exactly ONE fetch path in the
    codebase: same SSRF guards, same redirect re-validation, same size cap and
    timeout, same non-thread-safe-lxml executor, same content-hash rule (a
    changed hash resets indexed_at inside upsert_web_page, which is what puts
    the page back in the embedding queue automatically).

    Four outcomes, because collapsing them to a boolean is what made the 304
    path unrepresentable (finding K5) — the docstring at the top of this module
    has described conditional refresh since it was written, and until
    2026-09-06 no code could produce one:

      "read"          the page was downloaded and stored;
      "not_modified"  the server answered our If-None-Match/If-Modified-Since
                      with 304. The stored copy is confirmed current. NOT a
                      failure: `refresh_failures` must not move, or a stable
                      page would back off exponentially for being stable;
      "blocked"       robots.txt says we may not read it. Also not a failure —
                      nothing is wrong with the page or with us — so it simply
                      comes round again at its ordinary TTL, by which time the
                      site may have changed its mind;
      "failed"        anything else.

    The stored validators ride out with the request. They are already in
    `_DUE_COLUMNS` — they were selected and then thrown away.

    EXCEPT WHEN THE PAGE NEEDS THE BODY (2026-09-06). REMOTE freshness and
    PROCESSING freshness are different questions, and a conditional request
    only answers the first:

    * A page below `EXTRACT_VERSION` is in this queue because our STORED TEXT
      is wrong, not because the site changed. Re-extraction needs the original
      HTML, which is not kept anywhere — so a 304, which returns no body, is
      the one answer that makes the repair impossible. The page would then sit
      below the version for ever, re-offered every six hours, politely, without
      ever being fixed. Those rows therefore go out UNCONDITIONALLY: no
      `If-None-Match`, no `If-Modified-Since`, because we genuinely want the
      bytes even if nothing changed.
    * A page below `CHUNKER_VERSION` needs no request AT ALL — the stored text
      is fine and only the vectors are mis-shaped. That is why chunk staleness
      is deliberately absent from `_DUE_SQL_V21`: it is repaired by
      `web_index.index_pending`, locally, and a 304 here leaves the page fully
      visible to it (`touch_web_page_unchanged` moves `fetched_at` and nothing
      else).

    Everything else keeps the conditional request — that is what makes ~2,300
    refreshes a day affordable and polite, and it is measured working in
    production. `extract_version` is absent from the row on a deployment where
    the V22 column does not exist yet; unknown means conditional, the polite
    default.
    """
    url = row.get("url") or ""
    if not url:
        return "failed"
    needs_body = _stale_extractor(row)
    try:
        from .engines.search import refetch_page

        result = await refetch_page(
            url,
            previous_hash=row.get("content_hash") or "",
            etag=row.get("etag") or "",
            last_modified=row.get("last_modified") or "",
            conditional=not needs_body,
        )
    except Exception:  # noqa: BLE001 — one bad page must not stop the cycle
        log.debug("refresh failed for %s", url[:120], exc_info=True)
        return "failed"
    if result is None:
        return "failed"
    if result.get("blocked"):
        return "blocked"
    if result.get("not_modified"):
        return "not_modified"
    if result.get("changed"):
        # Only stamp last_changed_at when the CONTENT moved — a page re-read
        # daily would otherwise look freshly authored every day, and ranking
        # would trust it more than it deserves.
        await db.run_in_thread(_mark_changed, int(row["id"]))
    return "read"


def _mark_changed(page_id: int) -> None:
    with db.connection() as con:
        con.execute("UPDATE web_pages SET last_changed_at = now() WHERE id = %s", (page_id,))


async def run_once() -> Dict[str, int]:
    """One cycle: index the backlog, refresh what is due, then drain one
    queued background crawl (a shared link or a research run's primary
    domains) — the crawl last, so the answer-serving work never queues
    behind a site walk."""
    done = {
        "indexed": 0, "refreshed": 0, "failed": 0, "crawled": 0,
        # A 304 costs one conditional request and no bandwidth; counting it
        # apart from "refreshed" is the only way to see whether conditional
        # requests are actually working in production (finding K5).
        "not_modified": 0,
        "blocked": 0,
    }

    started = time.perf_counter()
    try:
        done["indexed"] = await web_index.index_pending(limit=40)
        metrics.worker_job("index", True, time.perf_counter() - started)
    except Exception:  # noqa: BLE001
        metrics.worker_job("index", False, time.perf_counter() - started)
        log.debug("index drain failed", exc_info=True)

    rows: List[Dict[str, Any]] = []
    if settings.web_knowledge_worker_enabled:
        started = time.perf_counter()
        try:
            rows = await db.run_in_thread(
                _due_pages, max(1, settings.web_refresh_max_pages_per_cycle)
            )
        except Exception:  # noqa: BLE001
            log.debug("could not read the refresh queue", exc_info=True)
            rows = []

    if rows:
        sem = asyncio.Semaphore(max(1, settings.web_refresh_concurrency))

        _OUTCOME_KEY = {
            "read": "refreshed",
            "not_modified": "not_modified",
            "blocked": "blocked",
            "failed": "failed",
        }

        async def one(row: Dict[str, Any]) -> None:
            async with sem:
                outcome = await _refresh_one(row)
                # Only a genuine failure backs the page off. A 304 and a
                # robots refusal both reschedule at the ordinary TTL with
                # `refresh_failures` reset, because neither says anything is
                # wrong with the page.
                await db.run_in_thread(
                    _schedule_next,
                    int(row["id"]),
                    _ttl_for(row),
                    failed=outcome == "failed",
                )
                done[_OUTCOME_KEY[outcome]] += 1

        await asyncio.gather(*(one(r) for r in rows), return_exceptions=True)
        metrics.worker_job("refresh", done["failed"] == 0, time.perf_counter() - started)

        # Newly-changed pages had their watermark cleared; embed them now
        # rather than waiting a whole cycle to become answerable.
        try:
            done["indexed"] += await web_index.index_pending(limit=40)
        except Exception:  # noqa: BLE001
            pass

    if settings.web_background_crawl_enabled:
        started = time.perf_counter()
        try:
            from .engines.crawl import run_queued_crawls

            done["crawled"] = await run_queued_crawls(max_jobs=1)
            if done["crawled"]:
                metrics.worker_job("crawl", True, time.perf_counter() - started)
        except Exception:  # noqa: BLE001
            metrics.worker_job("crawl", False, time.perf_counter() - started)
            log.debug("crawl queue drain failed", exc_info=True)

    # Index hygiene last (ADR-0001 D8): compaction on a cadence, the ANN
    # index once the table is big enough, and the self-heal for a deleted
    # index directory. Never on a request; failures are one log line.
    started = time.perf_counter()
    try:
        upkeep = await web_index.maintain()
        done["healed"] = int(upkeep.get("healed") or 0)
        # How much of the corpus still carries chunks from an older chunker.
        # A repair that needs no fetch is invisible in every other counter
        # here, so without this a chunker migration would drain (or stall)
        # unobserved.
        done["chunk_repair_pending"] = int(upkeep.get("stale_chunk_pages") or 0)
        if upkeep.get("optimized") or upkeep.get("indexed") or upkeep.get("healed"):
            metrics.worker_job("index_maintain", True, time.perf_counter() - started)
    except Exception:  # noqa: BLE001
        metrics.worker_job("index_maintain", False, time.perf_counter() - started)
        log.debug("index maintenance failed", exc_info=True)
    return done


async def _sleep_or_kick(seconds: float) -> None:
    """Sleep the interval, or less when kick() says there is work now."""
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    _wake.clear()
    try:
        await asyncio.wait_for(_wake.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _loop() -> None:
    global _wake
    _wake = asyncio.Event()
    # A short delay before the first cycle: startup already has a migration,
    # a model handshake and the first requests competing for the box.
    await _sleep_or_kick(45)
    # Jobs a restart interrupted go back to the queue before anything runs.
    try:
        requeued = await db.run_in_thread(db.requeue_interrupted_web_crawls)
        if requeued:
            log.info("crawl queue: %d interrupted job(s) requeued", requeued)
    except Exception:  # noqa: BLE001
        log.debug("could not requeue interrupted crawls", exc_info=True)
    while True:
        try:
            result = await run_once()
            if any(result.values()):
                log.info(
                    "web knowledge worker: indexed=%(indexed)d refreshed=%(refreshed)d "
                    "unchanged=%(not_modified)d blocked=%(blocked)d "
                    "failed=%(failed)d crawled=%(crawled)d "
                    "rechunk_pending=%(chunk_repair_pending)d",
                    {"chunk_repair_pending": 0, **result},
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop outlives any one cycle
            log.warning("web knowledge worker cycle failed", exc_info=True)
        # A kicked cycle that found a job runs the next one promptly too:
        # a research run can queue three domains at once.
        try:
            pending = await db.run_in_thread(db.web_crawl_queue_counts)
        except Exception:  # noqa: BLE001
            pending = {"queued": 0}
        if pending.get("queued"):
            await _sleep_or_kick(5)
        else:
            await _sleep_or_kick(max(30, settings.web_worker_interval_s))


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
