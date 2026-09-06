"""Drain the web-index backlog in bounded, resumable batches.

Runs inside the orchestrator container (``/app``), where the data volume, the
embedding service and the database are reachable:

    docker exec -it sf-local-ai-orchestrator-1 \\
        python -m tools.reindex_incremental --dry-run
    docker exec -it sf-local-ai-orchestrator-1 \\
        python -m tools.reindex_incremental --max-wall-clock-s 300

WHY THIS EXISTS. Raising ``web_index._MAX_CHUNKS_PER_PAGE`` and bumping
``web_index.CHUNKER_VERSION`` puts EVERY stored page into the V24 re-chunk
queue at once (2,208 pages / ~16k chunks on this deployment). Two things
already drain it, and this tool replaces neither:

* the live refresh worker — ``index_pending(limit=40)`` once per
  ``WEB_WORKER_INTERVAL_S`` (300 s). Correct, but ~21 hours for work that needs
  no network at all, during which the index holds two chunk shapes.
* ``tools.reindex_web build`` — a whole NEW directory, then a swap. That is the
  right tool when the index must be rebuilt from scratch; it is the wrong one
  here, because the pages are already indexed and only need re-chunking, and it
  costs a second copy of the index plus an env change plus ``adopt``.

This tool is the third thing: drive the SAME incremental repair the worker
runs, in the operator's foreground, faster than 40-pages-per-5-minutes but
under limits that keep it off the interactive path.

WHAT IT MAY TOUCH. Exactly what ``web_index.index_pending`` touches — the web
LanceDB directory (``LANCEDB_WEB_DIR``, table ``web_chunks``) and the two
watermark columns ``web_pages.indexed_at`` / ``web_pages.chunk_version``. It
reimplements no chunking and no embedding: ``index_pending`` is imported and
called, so this driver cannot drift from what the worker would have written.
It refuses to start when the configured web directory overlaps the SALESFORCE
corpus (``web_index.assert_not_salesforce``, both directions).

THE CHECKPOINT IS ALREADY THERE — VERIFIED, NOT INVENTED.
There is deliberately no progress file, no cursor and no second store. The
queue and the checkpoint are the same PostgreSQL rows:

* ``db.get_unindexed_web_pages`` selects on
  ``(indexed_at IS NULL OR chunk_version < CHUNKER_VERSION) AND text <> ''``
  (db.py:2513) — so a page LEAVES the queue only when its row says so.
* ``index_pending`` stamps ``db.mark_web_pages_indexed(page_ids,
  CHUNKER_VERSION)`` only AFTER ``table.add(rows)`` has returned
  (web_index.py:622-628), inside the same call, and the module says why: "a
  crash between the two re-does the page, which is free; the reverse loses it
  silently."
* the write is delete-then-insert keyed by ``page_id`` under the cross-process
  ``write_lock`` (web_index.py:616-622), so re-doing a page REPLACES its
  chunks. A resumed run cannot duplicate a row.
* thin pages (no chunkable text) are stamped too (web_index.py:598-604), so
  they leave the queue instead of being re-read every batch.

Consequences, and they are the whole reason this tool needs no state of its
own: a ``kill -9`` between batches loses nothing; a ``kill -9`` mid-batch loses
at most that batch's embedding work, which the next run redoes; and running
this while the live worker is also draining is safe, because both take the same
``flock`` and both stamp the same rows. Re-running the command IS the resume.

Observed in production 2026-09-06 (DEPLOYMENT-PLAN.md, not measured here): the
ad-hoc predecessor of this tool was interrupted deliberately after 3 batches
and, on resume, the LanceDB row and distinct-page counts were unchanged and its
220 repaired pages were not redone.

Re-verified for THIS tool on 2026-09-07, against an isolated corpus with the
embedding service replaced by a local fake: a real ``SIGTERM`` mid-drain
stopped it after the batch in flight (exit 143, 2 of 7 pages done), and the
resumed run finished with ``rows`` equal to the chunk count recomputed from
``web_pages.text`` and ``distinct_pages`` equal to the page count — no
duplicate, no gap.

LIMITS ARE CHECKED BETWEEN BATCHES, NEVER INSIDE ONE. A wall-clock budget that
could abandon a batch mid-flight would throw away embedding already paid for
and leave the vectors unstamped, so the real bound is
``budget + one batch``. Keep ``--batch-pages`` modest and that granularity
stays small; the flag exists for exactly that trade.

That trade gets sharper when ``_MAX_CHUNKS_PER_PAGE`` goes up. At the cap of 64
a page contributes at most 64 chunks, so 20 pages is a bounded burst; at 1,024
one page alone can, and the overshoot past ``--max-chunks`` — plus the vectors
held in memory for one batch, which is what VmHWM below will show — grows with
it. Lower ``--batch-pages`` when the cap is raised, rather than trusting the
chunk budget to bound a batch it cannot see inside.

Line numbers quoted in this file were checked on 2026-09-07. Everything below
``_MAX_CHUNKS_PER_PAGE`` in ``web_index.py`` shifts when that constant's
comment block changes; the function names do not.

NUMBERS THIS TOOL PRINTS BUT THIS FILE DOES NOT CLAIM. Every figure that
touches the GPU or the embedding service has to come from a real run, and one
run is enough to get all of them:

* **peak RSS on the real corpus.** Measured here only against a 7-page fixture
  with 4-dimension fake vectors: 39.8 MiB with no batch run, 186-194 MiB once
  LanceDB was loaded and writing. Production vectors are 1,024-dimension and a
  batch at a raised chunk cap holds far more of them, so treat those numbers as
  a floor, not an estimate.
* **chunks/s against the live embedding service**, which sets how long the
  whole V24 backlog takes. Prior recorded observation (DEPLOYMENT-PLAN.md,
  2026-09-06): 14,542 chunks in ~4.5 minutes with the cap at 64.
* **what a drain does to interactive TTFT** while it runs — the reason
  ``--pause-s`` exists at all.

``--max-chunks 500`` gives all three in well under a minute and leaves the rest
of the queue for the next run.

EXIT CODES.
  0  the queue drained, OR the run stopped on a limit the operator configured
     (wall clock / chunk budget) with work remaining — an intended stop.
  1  stalled: batches ran, no chunks were written and the queue did not shrink.
     This is the failure that would otherwise be invisible, because
     ``index_pending`` never raises — it logs and returns 0 (web_index.py:635),
     so a dead embedding service and a finished queue look identical from the
     outside. They are told apart here by the queue depth, not by the return.
     `index_pending` raising at all is the same class of stop and reported as
     `failed`: it is documented not to, and if it ever does the run still
     prints its report rather than dying in a traceback, because the peak-RSS
     reading below exists nowhere else.
  2  refused to start (Salesforce overlap, web memory disabled, bad flags).
  130/143  interrupted by SIGINT/SIGTERM with work remaining (128 + signal).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

if os.path.isdir("/app"):
    sys.path.insert(0, "/app")

from app import db, web_index  # noqa: E402
from app.config import settings  # noqa: E402

#: Pages per `index_pending` call. The live worker uses 40; this is lower on
#: purpose. It is the size of the work a stop cannot interrupt (see LIMITS
#: above) and the size of the embedding burst that queues in front of an
#: interactive query, so the conservative default trades a little throughput
#: for a smaller blast radius. Raise it when the box is otherwise idle.
DEFAULT_BATCH_PAGES = 20

#: Wall clock for the whole run. Five minutes is one refresh-worker interval:
#: long enough to be worth starting, short enough that an operator who walked
#: away has not left a drain running against a shared embedding service.
DEFAULT_MAX_WALL_CLOCK_S = 300.0

#: Chunks (= embedding calls' worth of text) per run. The second budget exists
#: because the first one cannot see cost: a wall clock spent on 200-char pages
#: and one spent on 180k-char pages bill the embedding service very
#: differently, and this is the number that maps to GPU time.
DEFAULT_MAX_CHUNKS = 5000

#: Seconds between batches. Non-zero by default: interactive chat shares the
#: embedding service with this drain (`llm.embed_texts`), and `index_pending`
#: already yields between its 64-text sub-batches for the same reason. Set 0
#: for a maintenance window with nobody on the box.
DEFAULT_PAUSE_S = 1.0

#: Consecutive batches that write nothing AND shrink nothing before the run is
#: declared stalled. More than one because a no-progress batch is not always a
#: fault: `index_pending` swallows `IndexBusy` when the live worker holds the
#: cross-process write lock for longer than its 20 s wait, and that is a queue,
#: not a failure. Three strikes plus the backoff below is ~6 s of tolerance.
_STALL_BATCHES = 3

#: Backoff after a no-progress batch, before the next attempt.
_STALL_BACKOFF_S = 2.0

EXIT_OK = 0
EXIT_STALLED = 1
EXIT_REFUSED = 2
EXIT_INTERRUPTED = 130

#: Stop reasons that are an operator's own instruction rather than a fault.
CONFIGURED_LIMITS = ("wall-clock", "chunk-budget")


@dataclass
class Limits:
    """Every bound on one run. All four are flags; all four have defaults."""

    batch_pages: int = DEFAULT_BATCH_PAGES
    max_wall_clock_s: float = DEFAULT_MAX_WALL_CLOCK_S
    max_chunks: int = DEFAULT_MAX_CHUNKS
    pause_s: float = DEFAULT_PAUSE_S


@dataclass
class Batch:
    """One `index_pending` call, as the progress line reports it."""

    number: int
    chunks: int
    remaining: int
    elapsed_s: float
    progressed: bool


@dataclass
class DrainReport:
    started_at: str = ""
    backlog_start: int = 0
    unindexed_start: int = 0
    stale_chunks_start: int = 0
    remaining: int = 0
    batches: int = 0
    chunks: int = 0
    seconds: float = 0.0
    stop_reason: str = "drained"
    error: str = ""
    signal_number: Optional[int] = None
    peak_rss_kib: Optional[int] = None
    rss_start_kib: Optional[int] = None
    lines: List[Batch] = field(default_factory=list)

    @property
    def done(self) -> int:
        """Pages that left the queue while this run was going.

        DELIBERATELY NOT "pages this process indexed": the live refresh worker
        drains the same queue and new pages join it, so this is the queue's
        movement, not a claim of authorship. `chunks` IS exactly ours — it is
        what `index_pending` returned to this process.
        """
        return max(0, self.backlog_start - self.remaining)

    @property
    def chunks_per_s(self) -> float:
        return self.chunks / self.seconds if self.seconds > 0 else 0.0

    @property
    def pages_per_min(self) -> float:
        return self.done * 60.0 / self.seconds if self.seconds > 0 else 0.0

    @property
    def exit_code(self) -> int:
        if self.remaining <= 0 or self.stop_reason == "drained":
            return EXIT_OK
        if self.stop_reason in CONFIGURED_LIMITS:
            return EXIT_OK
        if self.stop_reason == "interrupted":
            return 128 + int(self.signal_number or signal.SIGINT)
        return EXIT_STALLED  # stalled, failed, or anything else with work left

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "backlog_start": self.backlog_start,
            "unindexed_start": self.unindexed_start,
            "stale_chunks_start": self.stale_chunks_start,
            "remaining": self.remaining,
            "done": self.done,
            "batches": self.batches,
            "chunks": self.chunks,
            "seconds": round(self.seconds, 3),
            "chunks_per_s": round(self.chunks_per_s, 2),
            "pages_per_min": round(self.pages_per_min, 1),
            "stop_reason": self.stop_reason,
            "error": self.error,
            "chunker_version": int(web_index.CHUNKER_VERSION),
            "peak_rss_kib": self.peak_rss_kib,
            "rss_start_kib": self.rss_start_kib,
            "exit_code": self.exit_code,
        }


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------


def queue_depth() -> tuple:
    """`(unindexed, stale_chunks)` — the two halves of `index_pending`'s queue.

    They are exactly the two disjoint halves of the selection predicate, which
    is why adding them is the queue depth and not an approximation:

        unindexed     indexed_at IS NULL          AND text <> ''
        stale_chunks  indexed_at IS NOT NULL AND chunk_version < V AND text <> ''

    `/health` reports the same pair as `pending_pages` / `rechunk_pending`
    (health.py:306-314), so a running drain can be watched from outside this
    process with no extra endpoint.
    """
    unindexed = int(db.count_unindexed_web_pages())
    stale = int(db.count_stale_chunk_pages(int(web_index.CHUNKER_VERSION)))
    return unindexed, stale


async def _queue_depth_async() -> tuple:
    unindexed = int(await db.run_in_thread(db.count_unindexed_web_pages))
    stale = int(
        await db.run_in_thread(
            db.count_stale_chunk_pages, int(web_index.CHUNKER_VERSION)
        )
    )
    return unindexed, stale


# ---------------------------------------------------------------------------
# resources
# ---------------------------------------------------------------------------


def _proc_status_kib(field_name: str) -> Optional[int]:
    """One `/proc/self/status` size field in KiB, or None where there is no procfs."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(field_name + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def peak_rss_kib() -> Optional[int]:
    """`VmHWM` — the kernel's own high-water mark for this process's RSS.

    The manager cannot get this from outside: by the time the container is
    inspected the process has exited, and sampling `docker stats` would have to
    catch the peak as it happens. The kernel keeps it for free.

    It is the WHOLE process, not the drain alone — but that is the right
    number for sizing the container, and most of it IS the drain: `lancedb` and
    `pyarrow` are imported lazily inside `web_index._open`, so they load only
    when a batch actually writes. `rss_start_kib` is printed beside it so the
    interpreter's own baseline is visible rather than hidden inside the total.
    """
    return _proc_status_kib("VmHWM")


def current_rss_kib() -> Optional[int]:
    return _proc_status_kib("VmRSS")


def _mib(kib: Optional[int]) -> str:
    return "unknown" if kib is None else f"{kib / 1024.0:.1f} MiB"


# ---------------------------------------------------------------------------
# the drain
# ---------------------------------------------------------------------------


async def drain(
    limits: Limits,
    *,
    should_stop: Optional[Callable[[], Optional[int]]] = None,
    on_batch: Optional[Callable[[Batch], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> DrainReport:
    """Run `web_index.index_pending` until the queue empties or a limit fires.

    `should_stop` returns a signal number (truthy) to stop cleanly after the
    current batch; `on_batch` receives each progress line as it happens rather
    than at the end, so a long run prints as it goes and a killed one has
    already reported everything it did.

    Never swallows a limit into silence: the caller gets `stop_reason` and the
    remaining depth, and the exit code is derived from those two.
    """
    started = clock()
    report = DrainReport(
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        rss_start_kib=current_rss_kib(),
    )
    unindexed, stale = await _queue_depth_async()
    report.unindexed_start = unindexed
    report.stale_chunks_start = stale
    report.backlog_start = unindexed + stale
    report.remaining = report.backlog_start

    stalls = 0
    while True:
        if report.remaining <= 0:
            report.stop_reason = "drained"
            break
        stopping = should_stop() if should_stop is not None else None
        if stopping:
            report.stop_reason = "interrupted"
            report.signal_number = int(stopping)
            break
        if report.chunks >= limits.max_chunks:
            report.stop_reason = "chunk-budget"
            break
        if clock() - started >= limits.max_wall_clock_s:
            report.stop_reason = "wall-clock"
            break

        try:
            written = int(await web_index.index_pending(limit=limits.batch_pages))
        except Exception as exc:  # noqa: BLE001
            # `index_pending` is documented never to raise, and the whole point
            # of this tool is to survive to print its report — the peak-RSS
            # reading is only available from inside this process, and losing it
            # to a traceback would mean running the drain again to get it.
            report.stop_reason = "failed"
            report.error = f"{type(exc).__name__}: {exc}"
            report.batches += 1
            break
        report.batches += 1
        report.chunks += written
        unindexed, stale = await _queue_depth_async()
        remaining = unindexed + stale
        # A batch of thin pages writes NO chunks and is still progress: they are
        # stamped and leave the queue. Judging progress by the return value
        # alone would call that a stall and exit non-zero on a healthy run.
        progressed = written > 0 or remaining < report.remaining
        report.remaining = remaining
        report.seconds = clock() - started
        line = Batch(
            number=report.batches,
            chunks=written,
            remaining=remaining,
            elapsed_s=report.seconds,
            progressed=progressed,
        )
        report.lines.append(line)
        if on_batch is not None:
            on_batch(line)

        if not progressed:
            stalls += 1
            if stalls >= _STALL_BATCHES:
                report.stop_reason = "stalled"
                break
            await asyncio.sleep(_STALL_BACKOFF_S)
            continue
        stalls = 0
        if limits.pause_s > 0 and remaining > 0:
            await asyncio.sleep(limits.pause_s)

    report.seconds = clock() - started
    report.peak_rss_kib = peak_rss_kib()
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class Refused(RuntimeError):
    """A precondition failed, so nothing ran. Exit 2, no traceback."""


def preflight() -> str:
    """Everything that must hold before a single page is embedded.

    Returns the web index directory. Raises `Refused` with an operator-shaped
    message otherwise — never a traceback, which reads as a crash when it is a
    configuration answer.
    """
    directory = web_index.lancedb_web_dir()
    try:
        web_index.assert_not_salesforce(directory)
    except RuntimeError as exc:
        raise Refused(str(exc)) from exc
    if not settings.web_memory_enabled:
        # `index_pending` returns 0 immediately when this is off (web_index.py:
        # 554), so without this check the run would report a stall — a fault —
        # for a deliberate configuration.
        raise Refused(
            "WEB_MEMORY_ENABLED is off, so web_index.index_pending returns "
            "without reading the queue. Nothing to drain."
        )
    return directory


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.reindex_incremental",
        description=(
            "Drain the web index re-chunk/index backlog in bounded, resumable "
            "batches. Progress is durable in web_pages.chunk_version, so a kill "
            "is safe and re-running the command is the resume."
        ),
    )
    parser.add_argument(
        "--batch-pages", type=_positive_int, default=DEFAULT_BATCH_PAGES,
        help=f"max pages per index_pending call, and the work a stop cannot "
             f"interrupt (default: {DEFAULT_BATCH_PAGES})",
    )
    parser.add_argument(
        "--max-wall-clock-s", type=_positive_float, default=DEFAULT_MAX_WALL_CLOCK_S,
        help=f"stop before starting a batch once this many seconds have passed "
             f"(default: {DEFAULT_MAX_WALL_CLOCK_S:.0f})",
    )
    parser.add_argument(
        "--max-chunks", type=_positive_int, default=DEFAULT_MAX_CHUNKS,
        help=f"stop once this many chunks have been embedded this run "
             f"(default: {DEFAULT_MAX_CHUNKS})",
    )
    parser.add_argument(
        "--pause-s", type=_non_negative_float, default=DEFAULT_PAUSE_S,
        help=f"sleep between batches so a long drain does not monopolise the "
             f"embedding service interactive chat shares (default: {DEFAULT_PAUSE_S})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report the queue and stop: no embedding, no writes, no locks",
    )
    return parser


def _print_header(directory: str, limits: Limits) -> None:
    sidecar = web_index.sidecar_chunker_version(directory)
    # A sidecar BELOW the code's version is the normal mid-migration state, not
    # a fault: `web_index.maintain()` advances it only once the backlog reaches
    # zero, because half-way through a repair the table really does hold both
    # shapes (web_index.py:282-320).
    shown = "absent (no index yet)" if sidecar is None else f"v{sidecar}"
    behind = sidecar is not None and sidecar < web_index.CHUNKER_VERSION
    print(f"web index: {directory} (table {web_index.TABLE})")
    print(
        f"  chunker: code v{web_index.CHUNKER_VERSION}, sidecar {shown}"
        + ("  <- advances when the backlog reaches 0" if behind else "")
    )
    print(
        f"  limits: batch-pages={limits.batch_pages} "
        f"max-wall-clock-s={limits.max_wall_clock_s:g} "
        f"max-chunks={limits.max_chunks} pause-s={limits.pause_s:g}"
    )


def _print_queue(unindexed: int, stale: int, limits: Limits) -> int:
    total = unindexed + stale
    batches = (total + limits.batch_pages - 1) // limits.batch_pages
    print(f"queue: {total} page(s)")
    print(f"  new or changed text (indexed_at IS NULL): {unindexed}")
    print(f"  stale chunks (chunk_version < {web_index.CHUNKER_VERSION}):    {stale}")
    print(f"  at --batch-pages {limits.batch_pages}: {batches} batch(es)")
    return total


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    limits = Limits(
        batch_pages=args.batch_pages,
        max_wall_clock_s=args.max_wall_clock_s,
        max_chunks=args.max_chunks,
        pause_s=args.pause_s,
    )
    try:
        directory = preflight()
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    _print_header(directory, limits)

    if args.dry_run:
        unindexed, stale = queue_depth()
        _print_queue(unindexed, stale, limits)
        print(
            "the live refresh worker also drains this queue, 40 pages every "
            f"{settings.web_worker_interval_s:g}s"
        )
        print("dry run: nothing was embedded, nothing was written")
        print(f"peak RSS {_mib(peak_rss_kib())} (VmHWM, whole process)")
        return EXIT_OK

    # A signal stops the run AFTER the current batch, so the batch's vectors
    # reach LanceDB and its pages get stamped. A second signal is left to the
    # default handler: an operator who insists gets an immediate death, and the
    # checkpoint makes that safe too (at most the in-flight batch is redone).
    caught: dict = {"signum": None}

    def _stop(signum, _frame):
        if caught["signum"] is None:
            caught["signum"] = signum
            print(
                f"\nsignal {signum}: stopping after the current batch "
                "(send it again to stop now)",
                file=sys.stderr,
            )
            signal.signal(signum, signal.SIG_DFL)

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _stop)
        except (ValueError, OSError):  # not the main thread; tests call drain()
            pass

    def _line(batch: Batch) -> None:
        print(
            f"batch {batch.number:>3} chunks+{batch.chunks:<5} "
            f"left={batch.remaining:<6} elapsed={batch.elapsed_s:6.1f}s"
            + ("" if batch.progressed else "   <- no progress")
        )

    try:
        report = asyncio.run(
            drain(limits, should_stop=lambda: caught["signum"], on_batch=_line)
        )
    except KeyboardInterrupt:  # the second signal, or a Ctrl-C before the first
        print("interrupted; progress up to the last completed batch is durable",
              file=sys.stderr)
        print(f"peak RSS {_mib(peak_rss_kib())} (VmHWM, whole process)")
        return EXIT_INTERRUPTED
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    _print_report(report)
    return report.exit_code


def _print_report(report: DrainReport) -> None:
    print(
        f"{report.stop_reason}: {report.batches} batch(es), {report.chunks} chunk(s) "
        f"in {report.seconds:.1f}s "
        f"({report.chunks_per_s:.1f} chunks/s, {report.pages_per_min:.0f} pages/min)"
    )
    print(
        f"progress: done={report.done} remaining={report.remaining} "
        f"of {report.backlog_start} page(s) at the start"
    )
    if report.remaining > 0:
        if report.stop_reason in CONFIGURED_LIMITS:
            print(
                f"stopping on the configured {report.stop_reason} with "
                f"{report.remaining} page(s) left — re-run the same command to "
                "continue where this stopped (the queue IS the checkpoint: "
                "web_pages.chunk_version)"
            )
        elif report.stop_reason == "interrupted":
            print(
                f"interrupted with {report.remaining} page(s) left; everything "
                "up to the last completed batch is durable — re-run to continue"
            )
        elif report.stop_reason == "failed":
            print(
                f"FAILED with {report.remaining} page(s) left: {report.error}. "
                "Nothing is lost — the queue is the checkpoint — but fix the "
                "cause before re-running.",
                file=sys.stderr,
            )
        else:
            print(
                f"STALLED with {report.remaining} page(s) left: "
                f"{_STALL_BATCHES} consecutive batches wrote nothing and the "
                "queue did not move. index_pending never raises — check the "
                "orchestrator log for 'web indexing failed' and the embedding "
                "service before re-running.",
                file=sys.stderr,
            )
    else:
        print(
            "the queue is empty; web_index.maintain() advances the sidecar's "
            "chunker_version on its next cycle"
        )
    print(
        f"peak RSS {_mib(report.peak_rss_kib)} (VmHWM, whole process; "
        f"{_mib(report.rss_start_kib)} at start)"
    )
    print("summary-json " + json.dumps(report.as_dict(), sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
